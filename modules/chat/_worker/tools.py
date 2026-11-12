"""Chat worker tool execution: flush/discard/park helpers, the chat tool-handler registry and its handlers."""

import asyncio
import contextlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from modules.chat import media
from modules.chat._worker.base import (
    MAX_SKILL_CALLS_PER_ROUND,
    MAX_TRIP_IMAGE_CALLS_PER_ROUND,
    MAX_USER_INPUT_REQUESTS_PER_TURN,
    ResponseNoLongerActive,
    _next_event_seq,
)
from modules.chat._worker.setup import (
    _ModelSetup,
)
from modules.chat._worker.state import (
    _add_event,
    _ClaimedRun,
    _FenceOutcome,
    _live_run_fence,
    _publish_status,
    _RunCtx,
    _RunStopped,
    _StreamState,
    _TurnInputs,
)
from modules.chat.db_query import (
    TOOL_QUERY_COLLECTED_DATA,
    execute_db_query_tool,
)
from modules.chat.generative_ui import (
    TOOL_READ_SKILL,
    TOOL_TRIP_IMAGES,
    skill_tool_result,
)
from modules.chat.generative_ui.tool_specs import (
    LOCAL_DATA_TOOLS,
    TEMPLATE_TOOLS,
    TOOL_REQUEST_USER_INPUT,
    TOOL_SET_THEME,
    RequestUserInputArgs,
)
from modules.chat.schemas import (
    AnswerContext,
)
from modules.chat.templates import run_template_tool
from modules.chat.todos import run_todo_tool
from modules.chat.ui_theme import publish_theme
from modules.chat.user_input import (
    InputOutcome,
    await_user_input,
    create_input_request,
    resolve_ttl,
)


async def _flush_pending(c: _RunCtx, st: _StreamState) -> bool:
    """Publish the text buffer and newest UI snapshot in one fenced transaction.

    Lock order privacy -> conversation -> run -> evidence. Reasoning precedes text, which precedes the UI
    snapshots, so a snapshot never outruns the prose it follows. Returns False when the run was cancelled or
    redacted (terminal state persisted, buffers dropped); True otherwise (including nothing to publish).
    """
    if not st.pending and not st.pending_reasoning and not st.pending_ui:
        return True
    async with _live_run_fence(c, check_evidence=True, budgeted=True) as (session, outcome, _run):
        if outcome is not _FenceOutcome.LIVE:
            st.drop_pending()
            return False
        if st.pending_reasoning:
            c.seq = await _next_event_seq(session, c.response_id, c.seq)
            _add_event(session, c, c.seq, "message.reasoning.delta", {"text": st.pending_reasoning})
        if st.pending:
            c.seq = await _next_event_seq(session, c.response_id, c.seq)
            _add_event(session, c, c.seq, "message.delta", {"text": st.pending})
        for part_snapshot in st.pending_ui.values():
            # One event per part; every snapshot carries its `part_id` so the client can merge by part.
            c.seq = await _next_event_seq(session, c.response_id, c.seq)
            _add_event(session, c, c.seq, "message.ui.delta", part_snapshot)
        await session.commit()
    st.accumulated_text += st.pending
    st.accumulated_reasoning += st.pending_reasoning
    st.drop_pending()
    st.last_flush = asyncio.get_running_loop().time()
    return True


async def _publish_ui_discards(c: _RunCtx, part_ids: list[str]) -> bool:
    """Publish one ``message.ui.discard`` per part whose streamed snapshots must be removed from the client.

    Runs under the live-run fence (same lock order as other mid-run publishes). Returns False when the run
    was cancelled meanwhile (terminal state already persisted), True otherwise.
    """
    async with _live_run_fence(c, check_evidence=False) as (session, outcome, _run):
        if outcome is not _FenceOutcome.LIVE:
            return False
        for discard_id in part_ids:
            c.seq = await _next_event_seq(session, c.response_id, c.seq)
            _add_event(session, c, c.seq, "message.ui.discard", {"part_id": discard_id})
        await session.commit()
    return True


async def _park_for_input(
    c: _RunCtx, args: RequestUserInputArgs, ttl_seconds: int
) -> InputOutcome | None:
    """Park the run on a user-input request and wait for the answer with no lock or transaction held.

    Publication happens inside the standard lock order (privacy -> conversation -> run, status must be
    ``streaming``) and the commit releases every lock before the wait. Returns the answered/declined
    outcome, or None when the run was cancelled or the request expired (nothing more is published,
    exactly like a Stop). Raises PrivacyFenceChanged / ResponseNoLongerActive like other publish sites.
    """
    async with _live_run_fence(c, check_evidence=False) as (session, outcome, parked_run):
        if outcome is not _FenceOutcome.LIVE:
            return None
        payload = await create_input_request(
            session, parked_run, args, ttl=resolve_ttl(ttl_seconds)
        )
        # create_input_request appended its own event; re-read the high-water mark so later local
        # events (and a concurrent submit's event) keep strictly increasing seqs.
        c.seq = await _next_event_seq(session, c.response_id, c.seq) - 1
        await session.commit()
    result = await await_user_input(c.session_factory, c.response_id, payload["request_id"])
    return result if result.outcome in ("answered", "declined") else None


async def _run_todos(c: _RunCtx, name: str, raw_args: str) -> str | None:
    """Run one todo/template/theme tool under the live-run fence; None when the run was cancelled meanwhile.

    ``run_todo_tool``/``run_template_tool`` commit their own writes, which also releases the fence locks
    taken here. ``set_theme`` appends its ``ui.theme`` event under the run lock and commits here.
    """
    async with _live_run_fence(c, check_evidence=False) as (session, outcome, _run):
        if outcome is not _FenceOutcome.LIVE:
            return None
        if name == TOOL_SET_THEME:
            theme_result = await publish_theme(session, c.response_id, raw_args or "{}")
            # The helper allocated its own seq; resync the local high-water mark (as _park_for_input does).
            c.seq = await _next_event_seq(session, c.response_id, c.seq) - 1
            await session.commit()
            return theme_result
        if name in TEMPLATE_TOOLS:
            return await run_template_tool(
                name, raw_args or "{}", session, c.conversation_id, c.response_id
            )
        return await run_todo_tool(
            name, raw_args or "{}", session, c.conversation_id, c.response_id
        )


@dataclass
class _TurnState:
    """Mutable cross-round state of the streaming/tool loop (what the nested closures used to share)."""

    messages_payload: list[dict[str, Any]]
    stream_iter: Any
    tools_active: bool
    max_parts: int
    max_ui_rounds: int
    input_ttl_seconds: int
    last_cancel_check: float
    route_decision: dict[str, Any] | None = None
    ui_parts: list[tuple[str, dict[str, str]]] = field(
        default_factory=list
    )  # (part_id, tool call) in stream order
    ui_rounds: int = 0  # model rounds that produced at least one widget
    skill_rounds: int = 0  # data-tool continuation rounds
    input_requests: int = 0  # request_user_input calls answered or rejected so far this turn
    tool_rounds: int = 0  # continuing tool rounds so far (see MAX_TOOL_ROUNDS_PER_TURN)
    synth_done: bool = False  # the closing no-tools synthesis round was already opened this turn


@dataclass(frozen=True)
class _ToolEnv:
    """Read-mostly view handed to tool handlers: run context, claimed run, turn inputs, model setup, loop state."""

    c: _RunCtx
    claimed: _ClaimedRun
    turn: _TurnInputs
    setup: _ModelSetup
    ts: _TurnState


class ChatToolHandler(Protocol):
    """A non-presentation chat tool the worker may answer between model rounds.

    New tools register an instance with ``register_chat_tool`` instead of editing the dispatch loop.
    ``available`` gates whether the tool is offered/answered this turn, ``round_cap`` bounds calls per
    round (handlers sharing one instance share the cap), ``data_round`` marks calls that consume the shared
    data-tool round budget, and ``run`` produces the tool-result content (raising ``_RunStopped`` when the
    run ended while the tool was about to run).
    """

    data_round: bool

    def available(self, env: _ToolEnv) -> bool:
        """Whether the tool may be answered in this turn."""
        ...

    def round_cap(self, env: _ToolEnv) -> int:
        """Maximum calls answered in one model round."""
        ...

    async def run(self, env: _ToolEnv, call: dict[str, str]) -> str:
        """Execute one call and return the tool-result message content."""
        ...


_TOOL_HANDLERS: dict[str, ChatToolHandler] = {}


def register_chat_tool(names: Iterable[str], handler: ChatToolHandler) -> None:
    """Route the given tool names to ``handler`` in the worker's tool dispatch (last registration wins)."""
    for name in names:
        _TOOL_HANDLERS[name] = handler


class _ReadSkillHandler:
    """``read_skill``: returns the requested skill text; always available."""

    data_round = True

    def available(self, env: _ToolEnv) -> bool:
        """Always offered."""
        return True

    def round_cap(self, env: _ToolEnv) -> int:
        """At most MAX_SKILL_CALLS_PER_ROUND per round."""
        return MAX_SKILL_CALLS_PER_ROUND

    async def run(self, env: _ToolEnv, call: dict[str, str]) -> str:
        """Return the skill text for the call arguments."""
        return skill_tool_result(call["arguments"])


class _TripImagesHandler:
    """``get_trip_stop_images``: remote widget media, gated by the owner flag and the local-only fence."""

    data_round = True

    def available(self, env: _ToolEnv) -> bool:
        """Only when remote widget media is permitted for this turn."""
        return env.setup.trip_images_on

    def round_cap(self, env: _ToolEnv) -> int:
        """One call per round (the tool fans out to 8 lookups)."""
        return MAX_TRIP_IMAGE_CALLS_PER_ROUND

    async def run(self, env: _ToolEnv, call: dict[str, str]) -> str:
        """Run the tool; egress is gated again inside it (owner flag + local-only) and never raises for model input."""
        return await media.run_trip_images_tool(
            call["arguments"],
            config=env.setup.ai_config,
            local_only=env.setup.local_only_turn,
            secret=env.c.settings.csrf_signing_secret.get_secret_value(),
            subject=f"{env.claimed.workspace_id}:{env.claimed.actor_id}",
        )


def _db_query_outcome(result: str) -> dict[str, int]:
    """Counts-only summary of a ``query_collected_data`` result for the ``tool.status`` detail (pure).

    ``{"rows": n, "sources": k}`` on success (``k`` = distinct providers among the rows, may be 0 when rows
    carry none) and ``{"error": 1}`` when the result is an error or unparseable. No row content is exposed.
    """
    try:
        data = json.loads(result)
    except ValueError:
        return {"error": 1}
    if not isinstance(data, dict) or data.get("error"):
        return {"error": 1}
    rows: list[Any] = []
    for key in ("rows", "observations", "documents"):
        part = data.get(key)
        if isinstance(part, list):
            rows.extend(part)
    providers = {r["provider"] for r in rows if isinstance(r, dict) and r.get("provider")}
    return {"rows": len(rows), "sources": len(providers)}


class _QueryCollectedDataHandler:
    """``query_collected_data``: scoped read-only database query bracketed by running/done status events."""

    data_round = True

    def available(self, env: _ToolEnv) -> bool:
        """Only when the DB query tool is enabled for this turn."""
        return env.setup.db_query_on

    def round_cap(self, env: _ToolEnv) -> int:
        """At most MAX_SKILL_CALLS_PER_ROUND per round."""
        return MAX_SKILL_CALLS_PER_ROUND

    async def run(self, env: _ToolEnv, call: dict[str, str]) -> str:
        """Execute the query in its own session; "done" is published even on error (no stale spinner)."""
        c = env.c
        try:
            parsed_db_args = json.loads(call["arguments"] or "{}")
            if not isinstance(parsed_db_args, dict):
                parsed_db_args = {}
        except json.JSONDecodeError:
            parsed_db_args = {}
        await _publish_status(c, TOOL_QUERY_COLLECTED_DATA, "running")
        web_run = env.turn.web_run
        # Web results hold the numbers right after the prompt's evidence; new DB evidence goes behind them.
        web_numbering = (
            (web_run.evidence_base, len(web_run.results))
            if web_run is not None and web_run.evidence_base is not None and web_run.results
            else None
        )
        outcome: dict[str, int] = {"error": 1}  # replaced on success; stays if the tool raises
        try:
            async with c.session_factory() as db_tool_session:
                result = await execute_db_query_tool(
                    db_tool_session,
                    parsed_db_args,
                    scope=c.scope,
                    multi_workspace_enabled=c.settings.multi_workspace_enabled,
                    expected_access_fence=c.access,
                    retrieval_context=env.turn.req_params,
                    allow_local_only=env.setup.local_only_turn,
                    evidence_list=cast(AnswerContext, env.turn.answer_context).evidence,
                    redis=c.redis,
                    web_numbering=web_numbering,
                )
            outcome = _db_query_outcome(result)
            return result
        finally:
            # "done" also on error so the client never keeps a stale spinner; the numeric detail tells
            # the step list whether it found rows, found none or failed. A run that ended meanwhile has
            # nothing left to tell.
            with contextlib.suppress(ResponseNoLongerActive):
                await _publish_status(c, TOOL_QUERY_COLLECTED_DATA, "done", detail=outcome)


class _LocalDataHandler:
    """Todo, template and theme tools; they run under the live-run fence and share one per-round cap."""

    data_round = True

    def available(self, env: _ToolEnv) -> bool:
        """Always offered."""
        return True

    def round_cap(self, env: _ToolEnv) -> int:
        """At most MAX_SKILL_CALLS_PER_ROUND across all local data tools."""
        return MAX_SKILL_CALLS_PER_ROUND

    async def run(self, env: _ToolEnv, call: dict[str, str]) -> str:
        """Run the tool; raises ``_RunStopped`` when the run was cancelled while the tool was about to run."""
        result = await _run_todos(env.c, call["name"], call["arguments"])
        if result is None:
            raise _RunStopped("Response was stopped before a local data tool ran")
        return result


class _RequestUserInputHandler:
    """``request_user_input``: parks the run until the user answers; bounded per turn, not per round."""

    data_round = False

    def available(self, env: _ToolEnv) -> bool:
        """Always offered."""
        return True

    def round_cap(self, env: _ToolEnv) -> int:
        """Remaining turn budget: MAX_USER_INPUT_REQUESTS_PER_TURN minus requests already answered/rejected."""
        return MAX_USER_INPUT_REQUESTS_PER_TURN - env.ts.input_requests

    async def run(self, env: _ToolEnv, call: dict[str, str]) -> str:
        """Validate the arguments (invalid ones never park), park, and return the answer tool result."""
        env.ts.input_requests += 1
        try:
            input_args = RequestUserInputArgs.model_validate_json(call["arguments"] or "{}")
        except ValueError as exc:
            # Invalid arguments never park the run; the model sees the error and continues.
            return json.dumps({"error": f"invalid request_user_input arguments: {str(exc)[:300]}"})
        outcome = await _park_for_input(env.c, input_args, env.ts.input_ttl_seconds)
        if outcome is None:
            raise _RunStopped(
                "Run ended while parked for user input"
            )  # terminal state already persisted
        return outcome.tool_result


register_chat_tool([TOOL_READ_SKILL], _ReadSkillHandler())


register_chat_tool([TOOL_TRIP_IMAGES], _TripImagesHandler())


register_chat_tool([TOOL_QUERY_COLLECTED_DATA], _QueryCollectedDataHandler())


register_chat_tool(LOCAL_DATA_TOOLS, _LocalDataHandler())


register_chat_tool([TOOL_REQUEST_USER_INPUT], _RequestUserInputHandler())

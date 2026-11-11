"""Chat worker model streaming: SSE line application, round consumption/classification and the tool loop."""

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from core.model_gateway.client import (
    CapabilityUnsupported,
)
from core.model_gateway.wire import wire_protocol_for
from modules.chat._worker.base import (
    _UI_MILESTONE_KEYS,
    CANCEL_CHECK_INTERVAL,
    MAX_SKILL_ROUNDS,
    REASONING_MAX_CHARS,
    STREAM_FLUSH_CHARS,
    STREAM_FLUSH_SECONDS,
    ResponseNoLongerActive,
    is_run_cancelled,
    logger,
)
from modules.chat._worker.settle import (
    _mark_cancelled,
)
from modules.chat._worker.setup import (
    _ModelSetup,
)
from modules.chat._worker.state import (
    _parse_sse_chunk,
    _publish_status,
    _RoundState,
    _RunCtx,
    _RunStopped,
    _SseChunk,
    _StreamState,
)
from modules.chat._worker.tools import (
    _TOOL_HANDLERS,
    _flush_pending,
    _publish_ui_discards,
    _ToolEnv,
    _TurnState,
)
from modules.chat.db_query import TOOL_QUERY_COLLECTED_DATA
from modules.chat.generative_ui import (
    TERMINAL_UI_TOOLS,
    ToolCallAccumulator,
    streaming_snapshot,
    ui_call_error,
)
from modules.chat.generative_ui.tool_specs import TOOL_READ_SKILL, TOOL_REQUEST_USER_INPUT

# Tools that already publish their own progress (query_collected_data) or park the run behind their own
# input UI (request_user_input); every other handled data tool gets a generic running/done step.
_SELF_REPORTING_TOOLS = frozenset({TOOL_QUERY_COLLECTED_DATA, TOOL_REQUEST_USER_INPUT})


def _round_terminals(
    ts: _TurnState, round_acc: ToolCallAccumulator
) -> list[tuple[str, dict[str, str]]]:
    """Presentation calls of the current round with part ids unique across the whole turn.

    The remaining part budget (turn cap minus parts of earlier rounds) bounds the round. Providers may
    reuse a tool-call id (or the ``part-<n>`` fallback) in a later round, so a colliding id gets a
    round prefix; the result is deterministic for one round, hence identical in streamed snapshots and
    the final payload.
    """
    used = {pid for pid, _ in ts.ui_parts}
    out: list[tuple[str, dict[str, str]]] = []
    for pid, call in round_acc.terminals(max(0, ts.max_parts - len(ts.ui_parts))):
        base, n = f"r{ts.ui_rounds + 1}-{pid}"[:58], 2
        while pid in used:
            pid = base if base not in used else f"{base}-{n}"
            n += 1
        used.add(pid)
        out.append((pid, call))
    return out


async def _apply_text_delta(
    c: _RunCtx, st: _StreamState, rs: _RoundState, chunk: _SseChunk
) -> None:
    """Buffer reasoning/content of one chunk and flush when due; raises ``_RunStopped`` if the flush stopped the run.

    Reasoning is capped at REASONING_MAX_CHARS (overflow dropped, ``reasoning_truncated`` set). A later
    generative-UI round that continues the answer gets a paragraph break so its first words do not glue onto
    the previous round's last sentence. Flushes at STREAM_FLUSH_CHARS or STREAM_FLUSH_SECONDS.
    """
    content_delta = chunk.content
    if isinstance(chunk.reasoning, str) and chunk.reasoning:
        room = REASONING_MAX_CHARS - len(st.accumulated_reasoning) - len(st.pending_reasoning)
        if len(chunk.reasoning) > room:
            st.reasoning_truncated = True  # drop the overflow; later chunks are dropped too
        st.pending_reasoning += chunk.reasoning[: max(room, 0)]
    if (
        content_delta
        and rs.text_start
        and len(st.accumulated_text) + len(st.pending) == rs.text_start
        and not (st.accumulated_text + st.pending)[-1:].isspace()
    ):
        content_delta = "\n\n" + content_delta  # "equator.Here's" otherwise
    if content_delta or st.pending_reasoning:
        st.pending += content_delta or ""
        loop = asyncio.get_running_loop()
        due = len(st.pending) + len(st.pending_reasoning) >= STREAM_FLUSH_CHARS or (
            loop.time() - st.last_flush >= STREAM_FLUSH_SECONDS
        )
        if due and not await _flush_pending(c, st):
            raise _RunStopped("Response stopped during streaming")


async def _apply_tool_fragments(
    c: _RunCtx, st: _StreamState, ts: _TurnState, rs: _RoundState, fragments: Any
) -> None:
    """Feed tool-call fragments and publish a streaming UI snapshot for each widget whose arguments advanced.

    Milestones are detected on the new argument tail only (re-scanning a 256 KB sandbox per chunk would make
    streaming quadratic); parsing is throttled to STREAM_FLUSH_SECONDS unless a milestone key just appeared.
    Raises ``_RunStopped`` if a snapshot flush stopped the run.
    """
    loop = asyncio.get_running_loop()
    rs.acc.feed(fragments)
    for part_id, term in _round_terminals(ts, rs.acc):
        part_st = rs.ui_state.setdefault(
            part_id,
            {
                "len": 0,
                "seen": set(),
                "parse": float("-inf"),
                "snap": None,
            },
        )
        args_so_far = term["arguments"]
        if len(args_so_far) == part_st["len"]:
            continue  # this chunk belonged to another call
        tail = args_so_far[max(0, part_st["len"] - 24) :]
        part_st["len"] = len(args_so_far)
        fresh = {k for k in _UI_MILESTONE_KEYS if k in tail} - part_st["seen"]
        part_st["seen"] |= fresh
        if fresh or loop.time() - part_st["parse"] >= STREAM_FLUSH_SECONDS:
            part_st["parse"] = loop.time()
            snap = streaming_snapshot(term["name"], args_so_far)
            if snap is not None and snap != part_st["snap"]:
                part_st["snap"] = snap
                st.pending_ui[part_id] = {**snap, "part_id": part_id}
                if not await _flush_pending(c, st):
                    raise _RunStopped("Response stopped during streaming")


async def _apply_sse_line(
    c: _RunCtx,
    setup: _ModelSetup,
    st: _StreamState,
    ts: _TurnState,
    rs: _RoundState,
    line: str,
) -> None:
    """Apply one stripped SSE line: text/reasoning deltas, the provider's reasoning details and tool fragments.

    Malformed chunks raise ``json.JSONDecodeError`` / ``AttributeError`` (ignored by the caller); a stopped
    run raises ``_RunStopped``.
    """
    if not line.startswith("data: "):
        return
    chunk = _parse_sse_chunk(line[6:])
    if chunk is None:
        return
    await _apply_text_delta(c, st, rs, chunk)
    details = chunk.details
    # Only the Anthropic translator emits the complete list once; other providers
    # (OpenRouter) stream fragments, and echoing one would fail signature checks.
    if (
        wire_protocol_for(setup.ai_config.gateway_kind).echoes_reasoning_details
        and isinstance(details, list)
        and details
    ):
        rs.reasoning_details = [d for d in details[:64] if isinstance(d, dict)]
    if chunk.tool_fragments:
        await _apply_tool_fragments(c, st, ts, rs, chunk.tool_fragments)


async def _consume_round(
    c: _RunCtx,
    setup: _ModelSetup,
    st: _StreamState,
    ts: _TurnState,
    rs: _RoundState,
) -> None:
    """Stream one model round into the buffers, checking the Redis cancel flag at most every CANCEL_CHECK_INTERVAL.

    Raises ``_RunStopped`` after persisting a Stop; ``CapabilityUnsupported`` propagates to the caller (it may
    fall back to text-only before the first chunk).
    """
    loop = asyncio.get_running_loop()
    async with contextlib.aclosing(cast("AsyncGenerator[str, None]", ts.stream_iter)) as lines:
        async for raw_line in lines:
            rs.started = True
            if loop.time() - ts.last_cancel_check >= CANCEL_CHECK_INTERVAL:
                ts.last_cancel_check = loop.time()
                if await is_run_cancelled(c.response_id, c.redis):
                    await _mark_cancelled(
                        c.response_id, c.session_factory, c.seq, c.privacy_fence, scope=c.job_scope
                    )
                    raise _RunStopped("Response stopped during streaming")
            line = raw_line.strip()
            if not line or line == "data: [DONE]":
                continue
            try:
                await _apply_sse_line(c, setup, st, ts, rs, line)
            except (json.JSONDecodeError, AttributeError):
                pass


@dataclass
class _RoundCalls:
    """A round's tool calls split into rendered widgets, invalid widgets and handler-answered data calls."""

    handled: list[dict[str, str]]
    round_parts: list[tuple[str, dict[str, str]]]
    part_of: dict[int, str]
    invalid_of: dict[int, str]


async def _classify_round(
    env: _ToolEnv,
    st: _StreamState,
    rs: _RoundState,
    calls: list[dict[str, str]],
) -> _RoundCalls:
    """Split a finished round's calls into widgets / invalid widgets / handled data calls.

    Presentation tools never end the turn: every call this response made (within the remaining part budget)
    is a part. Invalid widget calls are not rendered; the model gets the validation error as the tool result
    (bounded by generative_ui_max_rounds). Snapshots streamed for an invalid call are discarded on the client
    (raises ``_RunStopped`` if the run was cancelled meanwhile).
    """
    c, ts = env.c, env.ts
    available = {n for n, h in _TOOL_HANDLERS.items() if h.available(env)}
    for extra in calls:
        if extra["name"] not in TERMINAL_UI_TOOLS and extra["name"] not in available:
            logger.warning(
                "Ignoring unknown tool call %r for run %s", extra["name"][:64], c.response_id
            )
    round_parts = _round_terminals(ts, rs.acc)
    capped_out = sum(call["name"] in TERMINAL_UI_TOOLS for call in calls) > len(round_parts)
    invalid_of = {
        id(x): err for _, x in round_parts if (err := ui_call_error(x["name"], x["arguments"]))
    }
    invalid_pids = [pid for pid, x in round_parts if id(x) in invalid_of]
    if invalid_pids and not await _publish_ui_discards(c, invalid_pids):
        raise _RunStopped("Response stopped while discarding invalid widgets")
    round_parts = [(pid, x) for pid, x in round_parts if id(x) not in invalid_of]
    part_of = {id(x): pid for pid, x in round_parts}
    if capped_out:
        logger.warning(
            "Generative UI part cap (%d) reached for run %s; extra widgets ignored",
            ts.max_parts,
            c.response_id,
        )
    handled: list[dict[str, str]] = []
    used: dict[
        int, int
    ] = {}  # handler identity -> calls taken this round (handlers may share a cap)
    for call in calls:
        handler = _TOOL_HANDLERS.get(call["name"])
        if id(call) in part_of or id(call) in invalid_of:
            handled.append(call)
        elif (
            handler is not None
            and handler.available(env)
            and used.get(id(handler), 0) < handler.round_cap(env)
        ):
            used[id(handler)] = used.get(id(handler), 0) + 1
            handled.append(call)
    return _RoundCalls(handled, round_parts, part_of, invalid_of)


def _bounded_continue(ts: _TurnState, rc: _RoundCalls, run_id: UUID) -> bool:
    """Apply the widget/data round caps; False means stop without another model round."""
    data_calls = [
        x
        for x in rc.handled
        if id(x) not in rc.part_of
        and id(x) not in rc.invalid_of
        and _TOOL_HANDLERS[x["name"]].data_round
    ]
    if rc.round_parts or rc.invalid_of:
        ts.ui_parts.extend(rc.round_parts)
        ts.ui_rounds += 1
        return not (
            len(ts.ui_parts) >= ts.max_parts or ts.ui_rounds >= ts.max_ui_rounds
        )  # caps reached
    if data_calls:
        # Data tools (skills, images, todos) share one extra-round bound; input requests have their own.
        if ts.skill_rounds >= MAX_SKILL_ROUNDS:
            logger.warning("Data-tool round limit reached for run %s; no widget", run_id)
            return False
        ts.skill_rounds += 1
    return True


async def _run_data_tool(env: _ToolEnv, call: dict[str, str]) -> str:
    """Run a handled data tool and publish ``tool.status`` running/done around it for the step list.

    ``read_skill`` carries the skill name as ``label``. ``done`` is published even when the tool raises so a
    client never keeps a stale step; a run that ended meanwhile has nothing left to tell. Handlers in
    ``_SELF_REPORTING_TOOLS`` publish their own progress. Handler errors (``_RunStopped``) propagate unchanged.
    """
    name = call["name"]
    handler = _TOOL_HANDLERS[name]
    if name in _SELF_REPORTING_TOOLS:
        return await handler.run(env, call)
    label: str | None = None
    if name == TOOL_READ_SKILL:
        try:
            parsed = json.loads(call["arguments"] or "{}")
        except ValueError:
            parsed = None
        skill = parsed.get("name") if isinstance(parsed, dict) else None
        label = skill if isinstance(skill, str) and skill else None
    await _publish_status(env.c, name, "running", label=label)
    try:
        return await handler.run(env, call)
    finally:
        with contextlib.suppress(ResponseNoLongerActive):
            await _publish_status(env.c, name, "done", label=label)


_TOOL_BUDGET_EXHAUSTED = json.dumps(
    {
        "error": "tool_budget_exhausted",
        "instruction": "Answer now from the evidence already returned; cite its [n] markers.",
    }
)


async def _answer_tool_calls(
    env: _ToolEnv,
    st: _StreamState,
    rs: _RoundState,
    rc: _RoundCalls,
    *,
    budget_exhausted: bool = False,
) -> None:
    """Append the assistant tool-call message and one tool result per handled call, in model order.

    Widget calls get a ``rendered`` stub (the payload is already on screen), invalid ones their validation
    error, data calls their handler's result (handlers may raise ``_RunStopped``). With ``budget_exhausted``
    data calls are not run: they get a ``tool_budget_exhausted`` result so the model answers from what it has.
    """
    ts = env.ts
    tool_calls = [
        {
            "id": x["id"] or f"call_{i}",
            "type": "function",
            "function": {"name": x["name"], "arguments": x["arguments"] or "{}"},
        }
        for i, x in enumerate(rc.handled)
    ]
    ts.messages_payload.append(
        {
            "role": "assistant",
            "content": st.accumulated_text[rs.text_start :] or None,
            "tool_calls": tool_calls,
            # Absent for providers that send none, so other kinds keep today's message shape.
            **({"reasoning_details": rs.reasoning_details} if rs.reasoning_details else {}),
        }
    )
    for tc, call in zip(tool_calls, rc.handled, strict=True):
        if id(call) in rc.invalid_of:
            content = rc.invalid_of[id(call)]
        elif id(call) in rc.part_of:
            # The payload is already on screen; echoing it back would only cost tokens.
            content = json.dumps({"status": "rendered", "part_id": rc.part_of[id(call)]})
        elif budget_exhausted:
            content = _TOOL_BUDGET_EXHAUSTED
        else:
            content = await _run_data_tool(env, call)
        ts.messages_payload.append({"role": "tool", "tool_call_id": tc["id"], "content": content})


async def _finish_round(env: _ToolEnv, st: _StreamState, rs: _RoundState) -> bool:
    """Flush the round, classify and answer its tool calls; False when the answer is complete.

    On True the next round's stream has been opened with the tool results appended. Raises ``_RunStopped``
    when a fence stopped the run.
    """
    c, ts = env.c, env.ts
    if not await _flush_pending(c, st):
        raise _RunStopped("Response stopped at round end")
    st.pending_ui = {}  # the completed payloads supersede any trailing partial snapshots
    rc = await _classify_round(env, st, rs, rs.acc.calls())
    if not rc.handled:
        return False  # a plain text round (or nothing we may act on): the answer is complete
    if not _bounded_continue(ts, rc, c.response_id):
        if rc.round_parts or rc.invalid_of or ts.synth_done:
            return False  # widget cap, or the synthesis round itself asked for tools again
        # Data-tool budget spent: close the turn with one no-tools round instead of dropping the calls
        # (which used to leave only the "let me search" narration as the persisted answer).
        ts.synth_done = True
        await _answer_tool_calls(env, st, rs, rc, budget_exhausted=True)
        ts.stream_iter = env.setup.open_stream(
            ts.messages_payload, env.setup.ui_tools, tool_choice="none"
        )
        return True
    await _answer_tool_calls(env, st, rs, rc)
    ts.stream_iter = env.setup.open_stream(ts.messages_payload, env.setup.ui_tools)
    return True


async def _stream_turn(env: _ToolEnv, st: _StreamState) -> None:
    """Stream tokens through ModelGateway, looping over tool-continuation rounds (phase 4).

    Deltas are buffered in worker memory (never published) and flushed at most every STREAM_FLUSH_SECONDS or
    STREAM_FLUSH_CHARS; each flush is one locked, revalidated transaction. A ``CapabilityUnsupported`` before
    the first chunk of a round retries text-only. Raises ``_RunStopped`` when the run ended.
    """
    c, ts, setup = env.c, env.ts, env.setup
    while True:
        rs = _RoundState(text_start=len(st.accumulated_text) + len(st.pending))
        try:
            await _consume_round(c, setup, st, ts, rs)
        except CapabilityUnsupported:
            if not ts.tools_active or rs.started:
                raise
            # Model/gateway cannot call tools: answer text-only (evidence-derived fallback widget later).
            logger.info("Tool calling unavailable for run %s; continuing text-only", c.response_id)
            ts.tools_active = False
            ts.messages_payload = setup.base_messages(False)
            ts.stream_iter = setup.open_stream(ts.messages_payload, None)
            continue
        if not await _finish_round(env, st, rs):
            return

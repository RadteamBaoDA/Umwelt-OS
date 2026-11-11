"""Chat worker finalization: citation validation, widget finalization and final message persistence."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select, update

from core import media_proxy
from modules.chat._worker.base import (
    _next_event_seq,
)
from modules.chat._worker.state import (
    _add_event,
    _evidence_still_valid,
    _fence_live_run,
    _FenceOutcome,
    _RunStopped,
    _StreamState,
)
from modules.chat._worker.tools import (
    _ToolEnv,
)
from modules.chat._worker.web import (
    _WebSearchRun,
)
from modules.chat.citations import (
    extract_supporting_quote,
    parse_citation_markers,
    renumber_citation_markers,
    validate_answer_citations,
)
from modules.chat.generative_ui import (
    fallback_generative_ui,
    finalize_generative_ui,
)
from modules.chat.generative_ui.tool_specs import TOOL_ANSWER
from modules.chat.models import (
    Conversation,
    Message,
    ResponseRun,
)
from modules.chat.schemas import (
    MAX_QUOTE_LENGTH,
    AnswerContext,
    AnswerContextRequest,
    Citation,
    WebCitation,
)


@dataclass(frozen=True)
class _CitedAnswer:
    """Validated final answer: renumbered text, persisted citations and the marker renumbering map."""

    final_answer: str
    valid_citations: list[dict[str, Any]]
    number_map: dict[int, int]


def _validate_document_citations(
    text: str,
    answer_context: AnswerContext,
    answer_request: AnswerContextRequest,
    doc_numbers: list[int],
    ui_parts: list[tuple[str, dict[str, str]]],
    total_count: int,
) -> tuple[Any, dict[int, int]]:
    """Validate document citations (prose markers plus widget-cited numbers) against the retrieved evidence.

    Only evidence the answer actually cites becomes a citation, in first-cited order. The widget tool call
    may cite evidence the prose never marks (render_table(citations=[1])): those numbers are candidates too
    and go through the same checks. Returns ``(ValidatedAnswer, number_map)`` where ``number_map`` maps
    context numbers to the kept citation numbers.
    """
    evidence_count = len(answer_context.evidence)

    def evidence_key(n: int) -> tuple[UUID, UUID]:
        """Dedup key (document version, chunk) of the evidence item cited as number ``n``."""
        item = answer_context.evidence[n - 1]
        return item.document_version_id, item.chunk_id

    ui_numbers = [n for _, call in ui_parts for n in _tool_citation_numbers(call, evidence_count)]
    candidate_numbers = list(dict.fromkeys([*doc_numbers, *ui_numbers]))
    first_by_key: dict[tuple[UUID, UUID], int] = {}
    for n in candidate_numbers:
        first_by_key.setdefault(evidence_key(n), n)
    unique_numbers = list(first_by_key.values())
    candidate_citations: list[Citation | dict[str, Any]] = [
        Citation(
            sourceType="document",
            sourceId=item.source_id,
            documentId=item.document_id,
            documentVersionId=item.document_version_id,
            chunkId=item.chunk_id,
            title=item.title,
            url=item.canonical_url,
            observedAt=item.observed_at,
            quote=extract_supporting_quote(text, item.content, citation_number=n),
        )
        for n, item in ((n, answer_context.evidence[n - 1]) for n in unique_numbers)
        if item.content.strip()
    ]
    all_markers = parse_citation_markers(text, max(total_count + 20, 20))
    allow_uncited_turn = (
        bool(text.strip())
        and not all_markers
        and not answer_request.selected_only
        and not answer_request.selected_refs
    )
    validated = validate_answer_citations(
        text,
        candidate_citations,
        answer_context.evidence,
        allow_uncited_conversational=allow_uncited_turn,
    )
    kept = {(c.documentVersionId, c.chunkId): i for i, c in enumerate(validated.citations, 1)}
    number_map = {n: kept[key] for n in candidate_numbers if (key := evidence_key(n)) in kept}
    if not doc_numbers and not number_map and validated.citations:
        # Preserve the [1] marker attached by post-hoc lexical attribution during renumbering.
        number_map[1] = 1
    return validated, number_map


def _web_citations_for(
    text: str,
    validated: Any,
    number_map: dict[int, int],
    doc_numbers: list[int],
    web_numbers: list[int],
    web_run: _WebSearchRun,
    evidence_count: int,
    total_count: int,
) -> tuple[str, list[dict[str, Any]]]:
    """Map cited web numbers to server-built ``WebCitation`` rows numbered after the kept documents.

    Owner ruling (review P2-5): a web-only answer is kept only when no document marker was dropped as invalid
    and no marker is out of range; otherwise the insufficient-evidence answer stands. Mutates ``number_map``
    and returns ``(answer_text, web_citations)``; one citation per URL (duplicate hits reuse its number).
    Each quote is the snippet truncated to ``MAX_QUOTE_LENGTH`` (snippets may be longer for the prompt).
    """
    answer_text = validated.answer
    web_citations: list[dict[str, Any]] = []
    web_only = (
        not validated.citations
        and all(n in number_map for n in doc_numbers)
        and all(
            n <= total_count for n in parse_citation_markers(text, total_count + 20)
        )  # "[2023]" is text
    )
    if validated.citations or web_only:
        answer_text = text if web_only else answer_text
        by_url: dict[str, int] = {}
        for n in web_numbers:
            hit = web_run.results[n - evidence_count - 1]
            if hit.url in by_url:
                number_map[n] = by_url[hit.url]
                continue
            number_map[n] = by_url[hit.url] = len(validated.citations) + len(web_citations) + 1
            web_citations.append(
                WebCitation(
                    url=hit.url,
                    title=hit.title,
                    # Prompt snippets run to SNIPPET_MAX (4000); the stored quote is a display excerpt
                    # capped by the schema, so cut it here instead of failing the whole run.
                    quote=hit.snippet[:MAX_QUOTE_LENGTH],
                    provider=web_run.provider,
                    retrievedAt=web_run.retrieved_at or datetime.now(UTC),
                ).model_dump(mode="json")
            )
    return answer_text, web_citations


def _validate_citations(
    text: str,
    answer_context: AnswerContext,
    answer_request: AnswerContextRequest,
    web_run: _WebSearchRun | None,
    ui_parts: list[tuple[str, dict[str, str]]],
) -> _CitedAnswer:
    """Validate the streamed answer's citations and renumber its markers (pure; no I/O).

    Combined numbering: evidence 1..k, web k+1..k+m. Documents are validated through
    ``validate_answer_citations`` (which returns a ValidatedAnswer); a cited web number becomes a server-built
    WebCitation numbered after the kept documents; uncited results are dropped.
    """
    web_results = list(web_run.results) if web_run is not None else []
    evidence_count = len(answer_context.evidence)
    total_count = evidence_count + len(web_results)
    cited_numbers = parse_citation_markers(text, total_count)
    doc_numbers = [n for n in cited_numbers if n <= evidence_count]
    # A rich answer may cite web results only inside its blocks; those count like inline markers.
    widget_web = [
        n
        for _, call in ui_parts
        if call["name"] == TOOL_ANSWER
        for n in _tool_citation_numbers(call, total_count, lowest=evidence_count + 1)
    ]
    web_numbers = list(
        dict.fromkeys([*(n for n in cited_numbers if n > evidence_count), *widget_web])
    )
    validated, number_map = _validate_document_citations(
        text,
        answer_context,
        answer_request,
        doc_numbers,
        ui_parts,
        total_count,
    )
    answer_text = validated.answer
    web_citations: list[dict[str, Any]] = []
    if web_numbers and web_run is not None:
        answer_text, web_citations = _web_citations_for(
            text,
            validated,
            number_map,
            doc_numbers,
            web_numbers,
            web_run,
            evidence_count,
            total_count,
        )
    final_answer = renumber_citation_markers(
        answer_text, number_map, max(total_count, 1 if number_map else 0)
    )
    valid_citations = [
        c.model_dump(mode="json", by_alias=True) for c in validated.citations
    ] + web_citations
    return _CitedAnswer(final_answer, valid_citations, number_map)


def _answer_images(env: _ToolEnv) -> dict[str, dict[str, str]]:
    """Map the turn's ``imgN`` ids to ``{src, alt, source_host}`` with owner-bound signed proxy paths.

    Tokens are bound to ``workspace:actor`` and expire (``media_proxy.TOKEN_TTL_SECONDS``); an image that cannot be
    signed (missing secret, URL off the allowlist) is simply absent, so ``render_answer`` drops it.
    """
    secret = env.c.settings.csrf_signing_secret.get_secret_value()
    subject = f"{env.claimed.workspace_id}:{env.claimed.actor_id}"
    out: dict[str, dict[str, str]] = {}
    for n, image in enumerate(env.setup.web_images, 1):
        try:
            token = media_proxy.sign_media_url(secret, image.url, subject=subject)
        except media_proxy.MediaError:
            continue
        out[f"img{n}"] = {
            "src": f"{media_proxy.PROXY_PATH}?token={token}",
            "alt": image.description,
            "source_host": image.source_host,
        }
    return out


def _finalize_widgets(
    env: _ToolEnv,
    final_answer: str,
    cited: _CitedAnswer,
) -> list[dict[str, Any]]:
    """Build the persisted Generative UI parts of the turn (pure).

    Tool-driven widgets map the model's evidence numbers through the answer's renumbering to validated
    citations, so an unvalidated number can never become an evidence reference; an invalid call drops only
    its own widget. Providers without tool support reuse the router decision classified before the first
    request (no second call) for a fallback widget. Tools offered with a plain-text answer yield no widgets.
    """
    ts, setup, turn = env.ts, env.setup, env.turn
    generative_uis: list[dict[str, Any]] = []
    if ts.ui_parts:
        widget_citations = {
            n: cited.valid_citations[m - 1]
            for n, m in cited.number_map.items()
            if 0 < m <= len(cited.valid_citations)
        }
        for part_id, call in ts.ui_parts:
            part = finalize_generative_ui(
                call["name"],
                call["arguments"],
                widget_citations,
                setup.local_only_turn,
                title_hint=" ".join(turn.user_prompt.split()),
                locale=setup.ui_locale,
                images=_answer_images(env) if call["name"] == TOOL_ANSWER else None,
                number_map=cited.number_map,
            )
            if part is not None:
                generative_uis.append({**part, "part_id": part_id})
    elif not ts.tools_active:
        fallback = fallback_generative_ui(
            turn.user_prompt,
            final_answer,
            cast(AnswerContext, turn.answer_context).evidence,
            local_only=setup.local_only_turn,
            decision=ts.route_decision,
        )
        if fallback is not None:
            generative_uis.append({**fallback, "part_id": "part-1"})
    return generative_uis


def _assistant_metadata(
    st: _StreamState,
    generative_uis: list[dict[str, Any]],
    route_decision: dict[str, Any] | None,
) -> dict[str, Any]:
    """Message ``metadata_json`` kwargs: reasoning, Generative UI parts and the router audit trail (pure).

    Returns ``{}`` when there is nothing to store so the column keeps its default.
    """
    generative_ui = (
        generative_uis[0] if generative_uis else None
    )  # first part, kept for older clients
    if generative_ui is None and route_decision is None and not st.accumulated_reasoning:
        return {}
    return {
        "metadata_json": {
            **(
                {
                    "reasoning": st.accumulated_reasoning,
                    **({"reasoning_truncated": True} if st.reasoning_truncated else {}),
                }
                if st.accumulated_reasoning
                else {}
            ),
            **(
                {"generative_ui": generative_ui, "generative_ui_parts": generative_uis}
                if generative_ui is not None
                else {}
            ),
            # Audit trail of the external router's choice (category + renderer only, no content).
            **(
                {
                    "viz_decision": {
                        "visualization": route_decision["visualization"],
                        "renderer": route_decision["renderer"],
                    }
                }
                if route_decision is not None
                else {}
            ),
        }
    }


async def _persist_final(
    env: _ToolEnv,
    st: _StreamState,
    final_answer: str,
    valid_citations: list[dict[str, Any]],
    generative_uis: list[dict[str, Any]],
) -> UUID:
    """Persist the completed assistant message, mark the run completed and publish the terminal events.

    One locked transaction (privacy -> conversation -> run -> evidence): a Stop or drifted evidence persists
    the cancelled/redacted state instead and raises ``_RunStopped``. Events are appended in order:
    ``message.citations``, one ``message.ui.done`` per part, then ``message.done``; committing releases every lock.

    Returns:
        The id of the persisted assistant message (for the post-answer title/suggestions step).
    """
    c, turn, setup = env.c, env.turn, env.setup
    async with c.session_factory() as session:
        outcome, live_run = await _fence_live_run(session, c, check_evidence=False)
        if outcome is not _FenceOutcome.LIVE:
            raise _RunStopped("Response was stopped before completion")
        conversation = await session.scalar(
            select(Conversation).where(Conversation.id == c.conversation_id)
        )
        if not await _evidence_still_valid(session, c, live_run):
            raise _RunStopped("Evidence was deleted before completion")
        model_ident = setup.mapping.model if setup.mapping else setup.role
        assistant_msg = Message(
            conversation_id=c.conversation_id,
            role="assistant",
            content=final_answer,
            model_identity=model_ident,
            citations=valid_citations,
            response_id=c.response_id,
            revision_of_message_id=turn.revision_of_message_id,
            **_assistant_metadata(st, generative_uis, env.ts.route_decision),
        )
        session.add(assistant_msg)
        await session.flush()
        ephemeral_flag = env.claimed.ephemeral
        expires_at = (
            conversation.expires_at if ephemeral_flag and conversation is not None else None
        )
        await session.execute(
            update(ResponseRun)
            .where(ResponseRun.id == c.response_id, ResponseRun.status == "streaming")
            .values(
                status="completed",
                assistant_message_id=assistant_msg.id,
                model_alias=setup.role,  # historical column name; stores the role
                model_name=model_ident,
                citations=valid_citations,
                ephemeral=ephemeral_flag,
                expires_at=expires_at,
                completed_at=func.now(),
            )
        )
        if valid_citations:
            c.seq = await _next_event_seq(session, c.response_id, c.seq)
            _add_event(session, c, c.seq, "message.citations", {"citations": valid_citations})
        for part in generative_uis:
            c.seq = await _next_event_seq(session, c.response_id, c.seq)
            _add_event(session, c, c.seq, "message.ui.done", part)
        done_payload: dict[str, Any] = {
            "text": final_answer,
            "citations": valid_citations,
            "model": model_ident,
            "status": "completed",
        }
        if generative_uis:
            done_payload["generative_ui"] = generative_uis[0]
            done_payload["generative_ui_parts"] = generative_uis
        c.seq = await _next_event_seq(session, c.response_id, c.seq)
        _add_event(session, c, c.seq, "message.done", done_payload)
        message_id = assistant_msg.id  # read before commit expires the instance
        await session.commit()
    return message_id


def _cited_lists(args: Any) -> list[Any]:
    """Every ``citations`` value of a tool call: the top-level one plus, for ``render_answer``, each block's
    and each stat item's."""
    if not isinstance(args, dict):
        return []
    found = [args.get("citations")]
    blocks = args.get("blocks")
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        found.append(block.get("citations"))
        items = block.get("items")
        if isinstance(items, list):
            found.extend(i.get("citations") for i in items if isinstance(i, dict))
    return found


def _tool_citation_numbers(
    ui_call: dict[str, str] | None, evidence_count: int, *, lowest: int = 1
) -> list[int]:
    """Return the in-range numbers (``lowest..evidence_count``) a presentation tool call cites.

    Reads ``args.citations`` and, for ``render_answer``, every block's/stat's ``citations``. Malformed JSON,
    non-list values, bools, non-ints and out-of-range numbers are dropped; the caller still validates every
    returned number against the retrieved evidence like a text marker.
    """
    if ui_call is None:
        return []
    try:
        args = json.loads(ui_call.get("arguments") or "{}")
    except ValueError:
        return []
    return [
        n
        for raw in _cited_lists(args)
        if isinstance(raw, list)
        for n in raw
        if type(n) is int and lowest <= n <= evidence_count
    ]

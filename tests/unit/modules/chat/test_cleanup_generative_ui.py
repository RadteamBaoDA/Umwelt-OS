"""Copied-evidence cleanup must drop Generative UI payloads that embed deleted-document evidence (no DB)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from modules.chat import public as chat
from modules.chat.generative_ui.streaming import streaming_snapshot
from tests.unit.modules._cleanup_scope import CTX, Recorder, admitted, evidence

MESSAGE_ID, RUN_ID, CONVERSATION_ID = uuid4(), uuid4(), uuid4()


class ScriptedSession(Recorder):
    """Recorder that serves one message, one terminal run and three stream events by target table."""

    def __init__(self, message: SimpleNamespace, events: dict[object, SimpleNamespace]) -> None:
        """Keep the rows the scripted statements resolve to (event rows are keyed by their id)."""
        super().__init__()
        self.message = message
        self.run = SimpleNamespace(status="completed", citations=[], retrieval_context={})
        self.events = events
        self._queue = iter(events.values())

    async def execute(self, statement: object, *_a: object, **_k: object) -> SimpleNamespace:
        """Return the page of candidate ids for whichever of the three tables is scanned."""
        text = self._record(statement)
        if type(statement).__name__ == "Update":
            # neutralize_ui_events: blank widget events of the run (all scripted events share RUN_ID).
            for event in self.events.values():
                if event.event_type in {"message.ui.delta", "message.ui.done"}:
                    event.data = {}
            return SimpleNamespace(all=list)
        if "chat_stream_events" in text:
            rows = [(eid, RUN_ID, CONVERSATION_ID) for eid in self.events]
        elif "chat_response_runs" in text:
            rows = [(RUN_ID, CONVERSATION_ID)]
        else:
            rows = [(MESSAGE_ID, CONVERSATION_ID)]
        return SimpleNamespace(all=lambda: rows)


    async def scalar(self, statement: object) -> object:
        """Answer one locked read; event reads consume the scripted events because the scan is id-ordered."""
        text = self._record(statement)
        if "chat_stream_events" in text:
            return next(self._queue)
        if "chat_response_runs" in text:
            return self.run
        if "chat_messages" in text:
            return self.message
        return SimpleNamespace()  # conversation parent: only its existence matters


def _copied(document_id: object) -> dict[str, object]:
    """An evidence object copied from the deleted document, as a widget would embed it."""
    return {"sourceType": "document", "documentId": str(document_id), "title": "Secret"}


async def test_cleanup_drops_generative_ui_from_messages_and_ui_events() -> None:
    scope = evidence()
    doc = scope.document_id
    message = SimpleNamespace(
        citations=[], response_id=RUN_ID,
        metadata_json={"keep": 1, "generative_ui": {"kind": "table", "citations": [_copied(doc)]}},
    )
    ids = {name: uuid4() for name in ("delta", "done", "final")}
    events = {
        # Real streaming output: it has no citations key and no document identifiers at all.
        ids["delta"]: SimpleNamespace(
            event_type="message.ui.delta", seq=1,
            data=streaming_snapshot("render_table", json.dumps({
                "title": "Secret", "columns": ["A"], "rows": [["secret-value"]], "citations": [1],
            })),
        ),
        ids["done"]: SimpleNamespace(
            event_type="message.ui.done", seq=2,
            data={"kind": "table", "table": {"rows": [["x"]]}, "citations": [_copied(doc)]},
        ),
        ids["final"]: SimpleNamespace(
            event_type="message.done", seq=3,
            data={"text": "answer", "generative_ui": {"kind": "table", "citations": [_copied(doc)]}},
        ),
    }
    session = ScriptedSession(message, events)
    with admitted(), patch("modules.memory.public.lock_export_privacy", AsyncMock()), \
            patch("modules.memory.public.bound_cleanup_lock_waits", AsyncMock()):
        progress = await chat.purge_document_copied_evidence_page(session, scope, **CTX)  # type: ignore[arg-type]

    assert progress.complete and progress.rows_changed >= 2  # done may be pre-blanked by the run-wide neutralize
    assert message.metadata_json == {"keep": 1}  # widget dropped whole; unrelated metadata kept
    assert events[ids["delta"]].data == {} and events[ids["done"]].data == {}
    assert "secret-value" not in json.dumps(events[ids["delta"]].data)
    assert "generative_ui" not in events[ids["final"]].data
    assert events[ids["final"]].data["text"] == "answer"


async def test_unrelated_generative_ui_is_left_untouched() -> None:
    scope = evidence()
    widget = {"kind": "table", "citations": [_copied(uuid4())]}  # another document's evidence
    message = SimpleNamespace(citations=[], metadata_json={"generative_ui": widget})
    session = ScriptedSession(message, {})
    with admitted(), patch("modules.memory.public.lock_export_privacy", AsyncMock()), \
            patch("modules.memory.public.bound_cleanup_lock_waits", AsyncMock()):
        progress = await chat.purge_document_copied_evidence_page(session, scope, **CTX)  # type: ignore[arg-type]
    assert progress.rows_changed == 0 and message.metadata_json == {"generative_ui": widget}


"""Owner-authenticated portable export aggregation and download routes."""

import csv
from datetime import UTC, datetime
from enum import StrEnum
import io
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner
from core.auth.models import AuthSession
from core.database import get_session
from modules.chat import public as chat_public
from modules.knowledge.documents import public as documents_public
from modules.memory import public as memory_public
from modules.sources import public as sources_public
from modules.tasks import public as tasks_public

router = APIRouter(prefix="/api/v1/exports", tags=["exports"])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
PAGE_SIZE = 100
MAX_EXPORT_BYTES = 32 * 1024 * 1024
DATASETS = (
    ("documents", "documents", documents_public),
    ("document_versions", "versions", documents_public),
    ("sources", "sources", sources_public),
    ("tasks", "tasks", tasks_public),
    ("conversations", "conversations", chat_public),
    ("messages", "messages", chat_public),
    ("memories", "memories", memory_public),
    ("memory_candidates", "candidates", memory_public),
)


class ExportFormat(StrEnum):
    """Name supported portable output encodings."""

    json = "json"
    markdown = "markdown"
    csv = "csv"


def _plain(value: BaseModel) -> dict[str, Any]:
    """Convert an allowlisted immutable owner DTO to JSON-safe portable fields."""
    return value.model_dump(mode="json")


def _encode_json(payload: dict[str, Any]) -> bytes:
    """Serialize the final credential-free portable manifest as compact UTF-8 JSON."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _render_markdown(payload: dict[str, Any]) -> bytes:
    """Render owner records as escaped JSON code blocks so arbitrary text stays intact."""
    lines = ["# BBD-OS data export", "", f"Created: {payload['created_at']}", ""]
    for dataset, records in payload["data"].items():
        lines.extend((f"## {dataset.replace('_', ' ').title()}", ""))
        if not records:
            lines.extend(("No records.", ""))
            continue
        for record in records:
            # Four-backtick fences remain valid even when a record contains triple backticks.
            lines.extend(("````json", json.dumps(record, ensure_ascii=False, indent=2), "````", ""))
    return "\n".join(lines).encode("utf-8")


def _render_csv(payload: dict[str, Any]) -> bytes:
    """Flatten the allowlisted dataset DTOs into dataset/type plus stable JSON-valued columns."""
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("dataset", "record_json"))
    for dataset, records in payload["data"].items():
        for record in records:
            writer.writerow((dataset, json.dumps(record, ensure_ascii=False, separators=(",", ":"))))
    return output.getvalue().encode("utf-8")


async def _collect_dataset(
    session: AsyncSession, owner_id: int, dataset: str, record_kind: str, public: Any,
    byte_budget: list[int],
) -> tuple[list[dict[str, Any]], dict[str, Any], tuple[Any, Any, list[BaseModel]]]:
    """Drain a public owner cursor while retaining bounded DTOs and private final fence receipts."""
    cursor = None
    records: list[dict[str, Any]] = []
    fences: list[BaseModel] = []
    snapshot = None
    metadata: dict[str, Any] | None = None
    while True:
        page = await public.export_page(
            session, owner_id=owner_id, record_kind=record_kind, limit=PAGE_SIZE, cursor=cursor,
        )
        if snapshot is None:
            snapshot = page.snapshot_at
            metadata = {
                "snapshot_at": page.snapshot_at.isoformat(),
                "snapshot_count": page.snapshot_count,
                "available": page.available,
                "omission_reason": page.omission_reason,
            }
            if hasattr(page, "privacy_persisted"):
                metadata.update({
                    "privacy_persisted": page.privacy_persisted,
                    "privacy_updated_at": page.privacy_updated_at,
                    "history_enabled": page.history_enabled,
                })
        elif page.snapshot_at != snapshot or page.snapshot_count != metadata["snapshot_count"]:
            raise HTTPException(status_code=409, detail="Export snapshot changed while reading owner pages")
        if page.payload_bytes > page.max_payload_bytes:
            raise HTTPException(status_code=503, detail="Owner export exceeded its declared page bound")
        for item in page.items:
            record = _plain(item)
            byte_budget[0] += len(_encode_json(record))
            if byte_budget[0] > MAX_EXPORT_BYTES:
                raise HTTPException(status_code=413, detail="Export exceeds the 32 MiB download bound")
            records.append(record)
        fences.extend(page.fences)
        if len(records) > 100_000:
            raise HTTPException(status_code=413, detail="Export contains too many records for one bounded download")
        cursor = page.next_cursor
        if cursor is None:
            break
    if snapshot is None or metadata is None:
        raise HTTPException(status_code=503, detail="Owner export page did not establish a snapshot")
    return records, metadata, (public, snapshot, fences)


async def _validate_final_fences(
    session: AsyncSession, owner_id: int, metadata: dict[str, dict[str, Any]],
    pending: dict[str, tuple[Any, Any, list[BaseModel]]],
) -> None:
    """Recheck every captured owner fence in bounded batches after all datasets are collected."""
    for dataset, (public, snapshot, fences) in pending.items():
        dataset_meta = metadata[dataset]
        # Disabled retained chat history must remain disabled from page read through publication.
        if dataset in {"conversations", "messages"} and not dataset_meta["available"]:
            privacy = await memory_public.read_export_privacy(session)
            if (privacy.store_conversation_history or privacy.persisted != dataset_meta.get("privacy_persisted")
                    or privacy.updated_at != dataset_meta.get("privacy_updated_at")):
                raise HTTPException(status_code=409, detail="Conversation export privacy changed; retry the download")
            continue
        batches = [fences[offset:offset + PAGE_SIZE] for offset in range(0, len(fences), PAGE_SIZE)] or [[]]
        for batch in batches:
            arguments: dict[str, Any] = {
                "session": session, "owner_id": owner_id,
                "record_kind": {
                    "document_versions": "versions",
                    "memory_candidates": "candidates",
                }.get(dataset, dataset),
                "snapshot_at": snapshot, "expected_snapshot_count": dataset_meta["snapshot_count"],
                "fences": batch,
            }
            if "privacy_persisted" in dataset_meta:
                arguments["privacy_persisted"] = dataset_meta["privacy_persisted"]
                arguments["privacy_updated_at"] = dataset_meta["privacy_updated_at"]
            validation = await public.validate_export_fences(**arguments)
            if not validation.valid:
                raise HTTPException(status_code=409, detail="Owner data changed during export; retry the download")


async def _build_export_response(
    output_format: ExportFormat,
    session: Session,
    owner: OwnerRead,
    response: Response,
) -> Response:
    """Build one credential-free bounded download and publish only after all owner fences pass."""
    collected: dict[str, list[dict[str, Any]]] = {}
    dataset_metadata: dict[str, dict[str, Any]] = {}
    pending: dict[str, tuple[Any, Any, list[BaseModel]]] = {}
    byte_budget = [0]
    for dataset, record_kind, public in DATASETS:
        records, metadata, final_fences = await _collect_dataset(
            session, owner.owner_id, dataset, record_kind, public, byte_budget,
        )
        collected[dataset] = records
        dataset_metadata[dataset] = metadata
        pending[dataset] = final_fences

    payload = {
        "format_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "supported_data": list(collected),
        "data": collected,
        "dataset_metadata": {
            dataset: {key: value for key, value in metadata.items() if key != "privacy_updated_at"}
            for dataset, metadata in dataset_metadata.items()
        },
        "limitations": [
            "Raw files, graph state, workflow secrets, runtime credentials, and deployment identity are not included in portable exports.",
            "Connector source configuration is omitted because provider-specific secret-free fields are not yet allowlisted.",
            "Conversation and message history is omitted when the owner privacy setting disables retention.",
            "This export is a portable owner-data projection, not a restorable instance backup.",
        ],
    }
    content_types = {
        ExportFormat.json: ("application/json; charset=utf-8", _encode_json),
        ExportFormat.markdown: ("text/markdown; charset=utf-8", _render_markdown),
        ExportFormat.csv: ("text/csv; charset=utf-8", _render_csv),
    }
    media_type, render = content_types[output_format]
    content = render(payload)
    if len(content) > MAX_EXPORT_BYTES:
        raise HTTPException(status_code=413, detail="Rendered export exceeds the 32 MiB download bound")
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    filename = f"umwelt-os-export.{output_format.value}"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    # Render and enforce the final byte cap before the last awaited owner check; no async work
    # occurs between this final fence pass and constructing the response body.
    await _validate_final_fences(session, owner.owner_id, dataset_metadata, pending)
    return Response(content=content, media_type=media_type, headers=dict(response.headers))


@router.get("/{output_format}")
async def download_export(
    output_format: ExportFormat,
    session: Session,
    owner: OwnerRead,
    response: Response,
) -> Response:
    """Return a no-store owner download and mark every route-level error response no-store too."""
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    try:
        return await _build_export_response(output_format, session, owner, response)
    except HTTPException as exc:
        headers = dict(exc.headers or {})
        headers.update({"Cache-Control": "private, no-store, max-age=0", "Pragma": "no-cache"})
        exc.headers = headers
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail="Owner data changed or could not be represented consistently; retry the download",
            headers={"Cache-Control": "private, no-store, max-age=0", "Pragma": "no-cache"},
        ) from exc

"""Bounded public Hugging Face author metadata snapshots."""

import asyncio
from datetime import UTC, datetime
import json
from urllib.parse import quote

import httpx

from modules.connectors.public import ConnectorConfig, ProviderCollectionPage, ProviderRateLimited
from modules.connectors.providers.feed_catalog import _parse_time, _retry_deadline, _selected_hash
from modules.knowledge.documents.public import ProviderRecordMetadata
from modules.ingestion.schemas import IngestionRecord
from modules.sources.schemas import ConnectorSource

_MAX_PAGE_BYTES = 10 * 1024 * 1024


async def _fetch_models(author: str) -> list[dict[str, object]]:
    """Fetch one fixed public author listing with bounded bytes and no pagination or redirects."""
    url = "https://huggingface.co/api/models?" + "&".join(
        (f"author={quote(author, safe='')}", "sort=lastModified", "limit=100", "full=true")
    )
    try:
        async with asyncio.timeout(30):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30), trust_env=False, follow_redirects=False, verify=True
            ) as client:
                async with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
                    if response.status_code == 429:
                        raise ProviderRateLimited(_retry_deadline(response.headers, datetime.now(UTC)))
                    if response.status_code == 403:
                        if (
                            response.headers.get("x-ratelimit-remaining") == "0"
                            or response.headers.get("retry-after") is not None
                        ):
                            raise ProviderRateLimited(_retry_deadline(response.headers, datetime.now(UTC)))
                        raise ValueError("huggingface_access_unavailable")
                    if response.status_code >= 400 or 300 <= response.status_code < 400:
                        raise ValueError("huggingface_provider_unavailable")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > _MAX_PAGE_BYTES:
                            raise ValueError("huggingface_response_too_large")
                    result = json.loads(body)
    except ProviderRateLimited:
        raise
    except (httpx.HTTPError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("huggingface_provider_unavailable") from None
    if not isinstance(result, list):
        raise ValueError("huggingface_response_invalid")
    models = [item for item in result if isinstance(item, dict)]
    if len(models) != len(result):
        raise ValueError("huggingface_response_invalid")
    return models


async def collect_huggingface_models(
    source: ConnectorSource, *, collected_at: datetime
) -> ProviderCollectionPage:
    """Collect at most 100 public model metadata rows for the configured author."""
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must be timezone-aware")
    config = ConnectorConfig.model_validate(source.configuration)
    author = config.huggingface_author
    if source.provider != "huggingface" or source.type != "api" or not author or config.history_mode != "returned_snapshot":
        raise ValueError("provider_scope_invalid")
    try:
        models = await _fetch_models(author)
    except ProviderRateLimited as exc:
        return ProviderCollectionPage(
            records=(), coverage="returned_snapshot", next_eligible_at=exc.next_eligible_at
        )
    truncated = len(models) > 100
    records: list[IngestionRecord] = []
    for model in models[:100]:
        model_id = model.get("id")
        if not isinstance(model_id, str) or not 1 <= len(model_id) <= 512:
            raise ValueError("huggingface_response_invalid")
        pieces = model_id.split("/")
        if len(pieces) != 2 or pieces[0].casefold() != author.casefold() or pieces[1] in {"", ".", ".."}:
            raise ValueError("huggingface_scope_mismatch")
        tags_raw = model.get("tags", [])
        if not isinstance(tags_raw, list) or any(not isinstance(tag, str) for tag in tags_raw):
            raise ValueError("huggingface_response_invalid")
        tags = [tag[:255] for tag in tags_raw][:100]
        pipeline_raw = model.get("pipeline_tag")
        sha_raw = model.get("sha")
        if pipeline_raw is not None and not isinstance(pipeline_raw, str):
            raise ValueError("huggingface_response_invalid")
        if sha_raw is not None and not isinstance(sha_raw, str):
            raise ValueError("huggingface_response_invalid")
        pipeline = pipeline_raw
        sha = sha_raw
        last_modified_raw = model.get("lastModified")
        created_raw = model.get("createdAt")
        if (last_modified_raw is not None and not isinstance(last_modified_raw, str)) or (
            created_raw is not None and not isinstance(created_raw, str)
        ):
            raise ValueError("huggingface_response_invalid")
        modified = None
        if isinstance(last_modified_raw, str):
            modified = _parse_time(last_modified_raw)
        created = None
        if isinstance(created_raw, str):
            created = _parse_time(created_raw)
        if (isinstance(last_modified_raw, str) and modified is None) or (
            isinstance(created_raw, str) and created is None
        ):
            raise ValueError("huggingface_timestamp_invalid")
        license_label = next((tag.split(":", 1)[1] for tag in tags if tag.startswith("license:") and len(tag) > 8), None)
        selected: dict[str, object] = {
            "id": model_id,
            "sha": sha,
            "lastModified": last_modified_raw if isinstance(last_modified_raw, str) else None,
            "createdAt": created_raw if isinstance(created_raw, str) else None,
            "tags": [tag for tag in tags_raw if isinstance(tag, str)] if isinstance(tags_raw, list) else [],
            "pipeline_tag": pipeline,
            "license": license_label,
        }
        digest = _selected_hash(selected)
        version = f"{sha}:{digest}" if sha else digest
        version = version[:255]
        content = f"{model_id}\nPipeline: {pipeline or 'unspecified'}\nTags: {', '.join(tags)}"
        source_fields: dict[str, object] = {"author": pieces[0][:1000], "tags": tags}
        source_fields_truncated = isinstance(tags_raw, list) and (
            len(tags_raw) > 100
            or any(isinstance(tag, str) and len(tag) > 255 for tag in tags_raw)
        )
        source_fields_truncated = source_fields_truncated or (
            isinstance(created_raw, str) and len(created_raw) > 64
        ) or (isinstance(last_modified_raw, str) and len(last_modified_raw) > 64)
        if pipeline is not None:
            source_fields["pipeline_tag"] = pipeline[:128]
            source_fields_truncated = source_fields_truncated or len(pipeline) > 128
        if created is not None and isinstance(created_raw, str):
            source_fields["created_at"] = created_raw[:64]
        if modified is not None and isinstance(last_modified_raw, str):
            source_fields["last_modified"] = last_modified_raw[:64]
        content_truncated = len(content) > 4000 or source_fields_truncated
        provider_record = ProviderRecordMetadata(
            provider="huggingface",
            identity=f"huggingface:model:{model_id}",
            provider_version=version,
            timestamp_basis="provider_modified" if modified else "collection",
            coverage="truncated" if truncated else "returned_snapshot",
            content_truncated=content_truncated,
            provider_modified_at=modified,
            license_label=license_label,
            source_fields=source_fields,
        )
        metadata: dict[str, object] = {
            "provider_record": provider_record.model_dump(mode="json", exclude_none=True),
            "title": model_id,
            "author": pieces[0],
            "tags": tags,
            "pipeline_tag": pipeline,
            "canonical_url": "https://huggingface.co/" + quote(model_id, safe="/"),
        }
        if created is not None and isinstance(created_raw, str):
            metadata["created_at"] = created_raw[:64]
        if modified is not None and isinstance(last_modified_raw, str):
            metadata["last_modified"] = last_modified_raw[:64]
        records.append(
            IngestionRecord(
                provider_id=f"huggingface:model:{model_id}",
                content=content[:4000],
                observed_at=modified or collected_at,
                version=version,
                metadata=metadata,
                collected_at=collected_at,
            )
        )
    return ProviderCollectionPage(
        records=tuple(records),
        coverage="truncated" if truncated else "returned_snapshot",
        next_eligible_at=None,
    )

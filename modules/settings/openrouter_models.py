"""OpenRouter model browse: allowlisted filter schema, query-parameter mapping and bounded response projection."""

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

_MODEL_ID = re.compile(r"[A-Za-z0-9@][A-Za-z0-9._:/@+-]{0,199}")
_PARAM = re.compile(r"[a-z_]{1,40}")
_MODALITIES = frozenset({"text", "image", "audio", "video", "file", "embeddings"})
_MAX_TOTAL = 1_000_000

_Param = Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,40}$")]
_Author = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
_Provider = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")]
_Endpoint = Literal[
    "/api/v1/chat/completions",
    "/api/v1/completions",
    "/api/v1/messages",
    "/api/v1/responses",
    "/api/v1/embeddings",
    "/api/v1/rerank",
    "/api/alpha/decisions",
    "/api/alpha/extractions",
    "/api/v1/videos",
    "/api/v1/audio/speech",
    "/api/v1/audio/transcriptions",
    "/api/v1/images",
    "/api/v1/batches",
]
_Sort = Literal[
    "most-popular",
    "newest",
    "top-weekly",
    "pricing-low-to-high",
    "pricing-high-to-low",
    "context-high-to-low",
    "throughput-high-to-low",
    "latency-low-to-high",
    "intelligence-high-to-low",
    "coding-high-to-low",
    "agentic-high-to-low",
    "design-arena-elo-high-to-low",
]
_Price = Annotated[float, Field(ge=0, le=10_000)]
_Days = Annotated[int, Field(ge=0, le=36_500)]
_Index = Annotated[float, Field(ge=0, le=1000)]
_Rate = Annotated[float, Field(ge=0, le=1)]


class OpenRouterModelFilters(BaseModel):
    """Allowlisted OpenRouter ``/models`` filters; unknown keys, bad enums, oversized lists and inverted ranges are 422."""

    model_config = ConfigDict(extra="forbid")

    output_modalities: Literal[
        "all",
        "text",
        "image",
        "embeddings",
        "audio",
        "video",
        "rerank",
        "decisions",
        "speech",
        "transcription",
    ] = "all"
    input_modalities: list[Literal["text", "image", "audio", "file"]] = Field(
        default_factory=list, max_length=4
    )
    category: (
        Literal[
            "programming",
            "roleplay",
            "marketing",
            "marketing/seo",
            "technology",
            "science",
            "translation",
            "legal",
            "finance",
            "health",
            "trivia",
            "academia",
        ]
        | None
    ) = None
    supported_parameters: list[_Param] = Field(default_factory=list, max_length=20)
    sort: _Sort = "newest"
    q: str = Field("", max_length=100)
    endpoints: list[_Endpoint] = Field(default_factory=list, max_length=13)
    context: int | None = Field(None, ge=1, le=10_000_000)
    min_price: _Price | None = None
    max_price: _Price | None = None
    min_output_price: _Price | None = None
    max_output_price: _Price | None = None
    min_age_days: _Days | None = None
    max_age_days: _Days | None = None
    arch: str | None = Field(None, pattern=r"^[A-Za-z0-9 ._-]{1,40}$")
    model_authors: list[_Author] = Field(default_factory=list, max_length=20)
    providers: list[_Provider] = Field(default_factory=list, max_length=20)
    distillable: bool | None = None
    zdr: bool = False
    region: Literal["eu", "us"] | None = None
    min_intelligence_index: _Index | None = None
    max_intelligence_index: _Index | None = None
    min_coding_index: _Index | None = None
    max_coding_index: _Index | None = None
    min_agentic_index: _Index | None = None
    max_agentic_index: _Index | None = None
    min_tool_success_rate: _Rate | None = None
    max_tool_success_rate: _Rate | None = None

    @model_validator(mode="after")
    def _ranges_ordered(self) -> "OpenRouterModelFilters":
        """Reject any ``min_X > max_X`` pair before any network I/O."""
        for name in type(self).model_fields:
            if name.startswith("min_"):
                low, high = getattr(self, name), getattr(self, "max_" + name[4:], None)
                if low is not None and high is not None and low > high:
                    raise ValueError(f"{name} must not exceed max_{name[4:]}")
        return self


def to_query_params(filters: OpenRouterModelFilters, offset: int, limit: int) -> dict[str, str]:
    """Map validated filters to upstream query parameters: lists comma-joined; None/empty/``zdr=False`` omitted.

    ``offset`` and ``limit`` are always present. Values are already allowlisted, so none can inject new keys.
    """
    params: dict[str, str] = {}
    for name, value in filters.model_dump().items():
        # ``distillable=False`` is a real filter ("false"); ``zdr=False`` just means unset.
        if value is None or (value is False and name == "zdr") or value == "" or value == []:
            continue
        if isinstance(value, bool):
            params[name] = "true" if value else "false"
        elif isinstance(value, list):
            params[name] = ",".join(value)
        else:
            params[name] = str(value)
    params["offset"], params["limit"] = str(offset), str(limit)
    return params


def _opt_int(value: Any) -> int | None:
    """Return ``value`` when it is a non-negative int (bool excluded), else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _price(value: Any) -> str | None:
    """Return a price string (<=32 chars, parses as a finite non-negative float), else None."""
    if not isinstance(value, str) or len(value) > 32:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return value if 0 <= number < float("inf") else None


def _modalities(value: Any) -> list[str]:
    """Reduce a modality list to the known set, sorted and unique."""
    return (
        sorted({m for m in value if isinstance(m, str) and m in _MODALITIES})
        if isinstance(value, list)
        else []
    )


def parse_openrouter_models(payload: Any, limit: int) -> tuple[list[dict[str, Any]], int, bool]:
    """Validate and bound an OpenRouter ``/models`` page into ``(models, total_count, has_next)``.

    Thin wrapper over ``parse_openrouter_page`` for callers that do not page by upstream offset.
    """
    models, total_count, has_next, _consumed = parse_openrouter_page(payload, limit)
    return models, total_count, has_next


def parse_openrouter_page(payload: Any, limit: int) -> tuple[list[dict[str, Any]], int, bool, int]:
    """Validate and bound an OpenRouter ``/models`` page into ``(models, total_count, has_next, consumed)``.

    At most ``limit`` unique models with strictly validated fields; malformed entries are skipped, never repaired.
    ``total_count`` is clamped to [0, 1_000_000] (falls back to the page size); ``has_next`` reflects ``links.next``.
    ``consumed`` is how many raw upstream entries were examined (valid, malformed or duplicate): the next page's
    offset must advance by this, not by ``len(models)``, or every skipped entry would be returned again.
    """
    body = payload if isinstance(payload, dict) else {}
    data = body.get("data")
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    consumed = 0
    for item in data if isinstance(data, list) else []:
        consumed += 1
        model_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id) or model_id in seen:
            continue
        seen.add(model_id)
        arch = item.get("architecture") if isinstance(item.get("architecture"), dict) else {}
        pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
        name = item.get("name")
        params = item.get("supported_parameters")
        models.append(
            {
                "id": model_id,
                "name": name if isinstance(name, str) and len(name) <= 200 else model_id,
                "created": _opt_int(item.get("created")),
                "context_length": _opt_int(item.get("context_length")),
                "input_modalities": _modalities(arch.get("input_modalities")),
                "output_modalities": _modalities(arch.get("output_modalities")),
                "pricing_prompt": _price(pricing.get("prompt")),
                "pricing_completion": _price(pricing.get("completion")),
                "supported_parameters": [
                    p for p in params if isinstance(p, str) and _PARAM.fullmatch(p)
                ][:64]
                if isinstance(params, list)
                else [],
            }
        )
        if len(models) >= limit:
            break
    total = body.get("total_count")
    total_count = (
        min(max(total, 0), _MAX_TOTAL)
        if isinstance(total, int) and not isinstance(total, bool)
        else len(models)
    )
    links = body.get("links")
    return models, total_count, bool(isinstance(links, dict) and links.get("next")), consumed

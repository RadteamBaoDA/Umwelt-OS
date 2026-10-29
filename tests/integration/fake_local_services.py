"""Fake local services for the disposable Compose harness (`uvicorn fake_local_services:app`).

One process plays the owner-controlled servers the connector e2e needs, all on one origin:
- `POST /mcp`: minimal stateless MCP streamable-HTTP server (JSON replies) with one read-only tool, `list_items`.
- `GET /ha/api/states/{entity_id}`: Home Assistant state subset (bearer token required).
- `GET /fhir/Observation`: FHIR R4 Observation search Bundle for patient `e2e-patient` (bearer token required).
- `GET /page`: static HTML page used to probe the browser crawler.
"""

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response

app = FastAPI(title="fake local services")
TOKEN = "e2e-local-token"
ITEMS = [{"id": f"item-{n}", "title": f"Local MCP item {n}"} for n in range(1, 4)]
HA_STATES = {"sensor.e2e_temperature": ("21.5", "°C", "E2E temperature"), "sensor.e2e_humidity": ("48", "%", "E2E humidity")}


def _require_bearer(authorization: str | None) -> None:
    """Reject requests that do not carry the fixed test bearer token."""
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="bad token")


@app.post("/mcp")
async def mcp(request: Request) -> Response:
    """Answer one MCP JSON-RPC message: initialize, tools/list, tools/call, ping; notifications get 202."""
    message: dict[str, Any] = await request.json()
    method, params, msg_id = message.get("method"), message.get("params") or {}, message.get("id")
    if msg_id is None:  # notification
        return Response(status_code=202)
    result: dict[str, Any]
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "fake-local", "version": "1.0"},
        }
    elif method == "tools/list":
        result = {"tools": [{
            "name": "list_items",
            "description": "Return a fixed list of items.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        }]}
    elif method == "tools/call" and params.get("name") == "list_items":
        result = {"content": [{"type": "text", "text": "3 items"}], "structuredContent": {"items": ITEMS}, "isError": False}
    elif method == "ping":
        result = {}
    else:
        return Response(
            content=_json({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "method not found"}}),
            media_type="application/json",
        )
    return Response(content=_json({"jsonrpc": "2.0", "id": msg_id, "result": result}), media_type="application/json")


def _json(value: object) -> str:
    """Serialize a JSON-RPC envelope."""
    import json

    return json.dumps(value)


@app.get("/ha/api/states/{entity_id}")
async def ha_state(entity_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Return one Home Assistant entity state, or 404 for an unknown entity."""
    _require_bearer(authorization)
    if entity_id not in HA_STATES:
        raise HTTPException(status_code=404)
    state, unit, name = HA_STATES[entity_id]
    now = datetime.now(UTC).isoformat()
    return {"entity_id": entity_id, "state": state, "last_updated": now, "last_changed": now,
            "attributes": {"unit_of_measurement": unit, "friendly_name": name}}


@app.get("/fhir/Observation")
async def fhir_observations(patient: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Return a Bundle of recent heart-rate Observations for the requested patient."""
    _require_bearer(authorization)
    now = datetime.now(UTC).isoformat()
    entries = [{"resource": {
        "resourceType": "Observation", "id": f"obs-{n}", "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4", "display": "Heart rate"}]},
        "subject": {"reference": f"Patient/{patient}"},
        "effectiveDateTime": now,
        "valueQuantity": {"value": 60 + n, "unit": "beats/min"},
    }} for n in range(1, 4)]
    return {"resourceType": "Bundle", "type": "searchset", "total": len(entries), "entry": entries}


@app.get("/page")
async def page() -> Response:
    """Serve a static HTML page for crawler probes."""
    return Response("<html><head><title>E2E local page</title></head><body><h1>E2E local page</h1><p>Hello crawler.</p></body></html>",
                    media_type="text/html")

"""Reusable UI template catalog and state tools for Open Generative UI."""

import uuid
from datetime import UTC, datetime
from typing import Any, TypedDict


class UITemplate(TypedDict, total=False):
    """Schema for a saved or built-in seed UI template."""

    id: str
    name: str
    description: str
    html: str
    data_description: str
    created_at: str
    version: int
    component_type: str | None
    component_data: dict[str, Any] | None


_WEATHER_SEED_HTML = """<div class="card" style="padding:16px;border:1px solid var(--line);border-radius:12px;background:var(--surface);color:var(--text);">
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <div>
      <h3 style="margin:0;font-size:16px;">San Francisco, CA</h3>
      <p style="margin:4px 0 0;font-size:12px;color:var(--muted);">Sample weather conditions</p>
    </div>
    <div style="font-size:28px;font-weight:700;color:var(--accent);">68°F</div>
  </div>
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px;font-size:12px;">
    <div><span style="color:var(--muted);">Humidity:</span> 62%</div>
    <div><span style="color:var(--muted);">Wind:</span> 11 mph W</div>
    <div><span style="color:var(--muted);">UV Index:</span> 4</div>
  </div>
  <button type="button" onclick="Websandbox.connection.remote.sendPrompt({ text: 'Explain the hourly forecast assumptions' })" style="margin-top:12px;padding:6px 12px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--text);cursor:pointer;">
    Ask about forecast
  </button>
</div>"""

_INVOICE_SEED_HTML = """<div class="card" style="padding:16px;border:1px solid var(--line);border-radius:12px;background:var(--surface);color:var(--text);">
  <div style="display:flex;justify-content:space-between;align-items:baseline;">
    <h3 style="margin:0;font-size:16px;">Invoice #INV-2048</h3>
    <span style="font-size:20px;font-weight:700;color:var(--accent);">$4,250.00</span>
  </div>
  <p style="margin:6px 0;font-size:13px;color:var(--muted);">Client: Acme Corp • Billing month: October • Due: Nov 15</p>
  <div style="display:flex;gap:8px;margin-top:12px;">
    <button type="button" onclick="Websandbox.connection.remote.sendPrompt({ text: 'Break down the line items for Invoice #INV-2048' })" style="padding:6px 12px;border-radius:8px;border:0;background:var(--accent);color:#fff;cursor:pointer;">
      Inspect line items
    </button>
  </div>
</div>"""

_DASHBOARD_SEED_HTML = """<div class="card" style="padding:16px;border:1px solid var(--line);border-radius:12px;background:var(--surface);color:var(--text);">
  <h3 style="margin:0;font-size:16px;">Quarterly KPI Overview</h3>
  <p style="margin:4px 0 12px;font-size:12px;color:var(--muted);">Illustrative sample metrics</p>
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;">
    <div style="padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);">
      <div style="font-size:11px;color:var(--muted);">Net Revenue</div>
      <div style="font-size:18px;font-weight:700;color:var(--chart-1);">$128.4K</div>
    </div>
    <div style="padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);">
      <div style="font-size:11px;color:var(--muted);">Active Accounts</div>
      <div style="font-size:18px;font-weight:700;color:var(--chart-2);">342</div>
    </div>
    <div style="padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);">
      <div style="font-size:11px;color:var(--muted);">Retention</div>
      <div style="font-size:18px;font-weight:700;color:var(--chart-3);">94.2%</div>
    </div>
  </div>
  <button type="button" onclick="Websandbox.connection.remote.sendPrompt({ text: 'Compare Q3 and Q4 KPI assumptions' })" style="margin-top:12px;padding:6px 12px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--text);cursor:pointer;">
    Compare quarters
  </button>
</div>"""

SEED_TEMPLATES: list[UITemplate] = [
    {
        "id": "seed-weather-001",
        "name": "Weather",
        "description": "Current weather conditions card with temperature, humidity, wind, and UV index",
        "html": _WEATHER_SEED_HTML,
        "data_description": "City name, date, temperature, condition, humidity, wind speed/direction, UV index",
        "version": 1,
    },
    {
        "id": "seed-invoice-001",
        "name": "Invoice Card",
        "description": "Compact invoice card with amount, client info, and action buttons",
        "html": _INVOICE_SEED_HTML,
        "data_description": "Title, amount, description, client name, billing month, invoice number, due date",
        "version": 1,
    },
    {
        "id": "seed-dashboard-001",
        "name": "Dashboard",
        "description": "KPI dashboard with metrics cards and bar chart for quarterly performance",
        "html": _DASHBOARD_SEED_HTML,
        "data_description": "Title, subtitle, KPI labels/values/changes, monthly bar chart data, legend items",
        "version": 1,
    },
]

TEMPLATE_USAGE_NOTE = (
    "This template HTML is a style reference only. When regenerating via "
    "generateSandboxedUi: move any <style> block content into the css parameter "
    "(the html parameter must not contain <style> blocks); put behavior in "
    "jsFunctions/jsExpressions; and send prompts through the sandbox bridge — "
    "await Websandbox.connection.remote.sendPrompt({ text }) — never a bare "
    "sendPrompt(...) call."
)


class _ToolDescriptor:
    """Lightweight callable tool wrapper compatible with LangChain tool inspection."""

    def __init__(self, func: Any) -> None:
        """Bind a Python function with `.name`, `.description`, `.func`, and `.invoke`."""
        self.func = func
        self.name = func.__name__
        self.description = (func.__doc__ or "").strip()
        self.__doc__ = func.__doc__
        self.__name__ = func.__name__

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Invoke the underlying function directly."""
        return self.func(*args, **kwargs)

    def invoke(self, input_data: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        """Invoke the underlying function using a dictionary of keyword arguments."""
        merged = {**(input_data or {}), **kwargs}
        return self.func(**merged)


def _save_template(
    name: str,
    description: str,
    html: str,
    data_description: str,
    runtime: Any,
) -> dict[str, Any]:
    """Save a generated UI as a reusable template in runtime state.

    Call this when the user wants to save a widget/visualization they liked for reuse later.

    Args:
        name: Short name for the template (e.g. "Invoice", "Dashboard").
        description: What the template displays or does.
        html: The raw HTML string of the widget to save as a template.
        data_description: Description of the data shape this template expects.
        runtime: Tool runtime holding `state`.
    """
    templates = list(runtime.state.get("templates", []))
    template: UITemplate = {
        "id": str(uuid.uuid4()),
        "name": name,
        "description": description,
        "html": html,
        "data_description": data_description,
        "created_at": datetime.now(UTC).isoformat(),
        "version": 1,
    }
    templates.append(template)
    runtime.state["templates"] = templates
    return {
        "templates": templates,
        "message": f"Template '{name}' saved successfully (id: {template['id']})",
    }


def _list_templates(runtime: Any) -> list[dict[str, Any]]:
    """List all saved UI templates, including built-in seed templates.

    Returns template summaries (id, name, description, data_description, version).
    """
    state_templates = list(runtime.state.get("templates", [])) if runtime and hasattr(runtime, "state") else []
    state_ids = {t["id"] for t in state_templates}
    templates = [*state_templates, *(s for s in SEED_TEMPLATES if s["id"] not in state_ids)]
    return [
        {
            "id": t["id"],
            "name": t["name"],
            "description": t["description"],
            "data_description": t["data_description"],
            "version": t["version"],
        }
        for t in templates
    ]


def _apply_template(runtime: Any, name: str = "", template_id: str = "") -> dict[str, Any]:
    """Retrieve a saved template's HTML so you can adapt it with new data.

    After calling this, generate a NEW widget in the same style and render via generateSandboxedUi.
    Also searches built-in seed templates by name or template_id.

    Args:
        runtime: Tool runtime holding `state`.
        name: The name of the template to apply (fallback if no pending_template).
        template_id: The ID of the template to apply (fallback if no pending_template).
    """
    state_templates = list(runtime.state.get("templates", [])) if runtime and hasattr(runtime, "state") else []
    state_ids = {t["id"] for t in state_templates}
    templates = [*state_templates, *(s for s in SEED_TEMPLATES if s["id"] not in state_ids)]

    pending = runtime.state.get("pending_template") if runtime and hasattr(runtime, "state") else None
    if isinstance(pending, dict) and pending.get("id"):
        template_id = str(pending["id"])

    if template_id:
        for t in templates:
            if t["id"] == template_id:
                return {
                    "name": t["name"],
                    "description": t["description"],
                    "html": t["html"],
                    "data_description": t.get("data_description", ""),
                    "usage_note": TEMPLATE_USAGE_NOTE,
                }
        return {"error": f"Template with id '{template_id}' not found"}

    if name:
        matches = [t for t in templates if t["name"].lower() == name.lower()]
        if matches:
            t = max(matches, key=lambda x: x.get("created_at", ""))
            return {
                "name": t["name"],
                "description": t["description"],
                "html": t["html"],
                "data_description": t.get("data_description", ""),
                "usage_note": TEMPLATE_USAGE_NOTE,
            }
        return {"error": f"No template named '{name}' found"}

    return {"error": "Provide either a name or template_id"}


def _delete_template(template_id: str, runtime: Any) -> dict[str, Any]:
    """Delete a saved UI template by ID from runtime state."""
    templates = list(runtime.state.get("templates", []))
    original_len = len(templates)
    templates = [t for t in templates if t["id"] != template_id]
    if len(templates) == original_len:
        return {"error": f"Template with id '{template_id}' not found"}
    runtime.state["templates"] = templates
    return {"templates": templates, "message": "Template deleted successfully"}


def _clear_pending_template(runtime: Any) -> dict[str, Any]:
    """Clear the pending_template from runtime state after applying it."""
    runtime.state["pending_template"] = None
    return {"pending_template": None, "message": "Pending template cleared"}


save_template = _ToolDescriptor(_save_template)
list_templates = _ToolDescriptor(_list_templates)
apply_template = _ToolDescriptor(_apply_template)
delete_template = _ToolDescriptor(_delete_template)
clear_pending_template = _ToolDescriptor(_clear_pending_template)

template_tools = [
    save_template,
    list_templates,
    apply_template,
    delete_template,
    clear_pending_template,
]

"""Standalone HTML5 document assembly for the widget MCP server.

Ported from CopilotKit/OpenIntelligentUI@8391a94 apps/mcp (MIT). The theme/SVG/form CSS below is a minimal
copy of the design system that lives in TypeScript at
apps/web/src/modules/chat/generative-ui/design-system/index.ts; keep token values in sync with it.
"""

import html as _html
import json

MAX_HTML_BYTES = 256 * 1024
MAX_TITLE_CHARS = 200

# No remote origins at all: assembled documents are self-contained and cannot fetch or submit anywhere.
CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'"
)

_CSS = """
:root{color-scheme:light;--bg:#f4f5f2;--surface:#fff;--text:#17211c;--muted:#69756e;--line:#dfe4df;--accent:#176a4a;--on-accent:#fff;--danger:#a82e35;
--p:var(--text);--s:var(--muted);--bg2:var(--surface);--b:var(--line);--t:var(--muted);--font-sans:system-ui,-apple-system,"Segoe UI",sans-serif;--radius:8px}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#141a17;--surface:#1d2621;--text:#edf3ee;--muted:#a1afa6;--line:#35433a;--accent:#77c69c;--on-accent:#102117;--danger:#ff9b9f}}
*{box-sizing:border-box;margin:0}
body{font:15px/1.6 var(--font-sans);color:var(--text);background:transparent;padding:8px}
svg text.t{font:400 14px var(--font-sans);fill:var(--p)}svg text.ts{font:400 12px var(--font-sans);fill:var(--s)}svg text.th{font:600 14px var(--font-sans);fill:var(--p)}
svg .box>rect,svg .box>circle,svg .box>ellipse{fill:var(--bg2);stroke:var(--b)}
svg .arr{stroke:var(--s);stroke-width:1.5;fill:none}svg .leader{stroke:var(--t);stroke-width:.5;stroke-dasharray:4 4;fill:none}
button{font:600 14px var(--font-sans);min-height:36px;padding:6px 14px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);color:var(--text);cursor:pointer}
button:hover{border-color:var(--accent)}button:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
input[type=text],input[type=number],input[type=email],input[type=search],textarea,select{font:inherit;min-height:36px;padding:6px 10px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);color:var(--text)}
input[type=range]{accent-color:var(--accent)}
"""

# Bridge: widget -> embedding page. The target origin is injected as a JSON string literal; never '*'.
_BRIDGE_JS = """
(function(){
  var TARGET=__TARGET__;
  function post(m){if(window.parent&&window.parent!==window)window.parent.postMessage(m,TARGET);}
  window.sendPrompt=function(t){t=typeof t==='string'?t:(t&&t.prompt)||'';t=String(t).trim();if(t)post({type:'send-prompt',prompt:t});};
  window.openLink=function(u){post({type:'open-link',url:String(u)});};
  var last=0;
  function report(){var h=Math.ceil(document.documentElement.getBoundingClientRect().height);if(h!==last){last=h;post({type:'widget-resize',height:h});}}
  if(typeof ResizeObserver==='function')new ResizeObserver(report).observe(document.documentElement);
  window.addEventListener('load',report);
})();
"""


def assemble_document(title: str, description: str, html: str, *, parent_origin: str) -> str:
    """Wrap an HTML fragment into a complete standalone HTML5 document.

    Args:
        title: Short document title; HTML-escaped and length-bounded.
        description: One-sentence summary stored in a meta tag (escaped).
        html: Self-contained fragment (inline style/script); UTF-8 size must be 1..MAX_HTML_BYTES.
        parent_origin: Exact origin the bridge posts messages to; must be a concrete http(s) origin.

    Returns:
        Document with strict CSP (no remote origins), theme/SVG/form CSS and the postMessage bridge.

    Raises:
        ValueError: On empty/oversized/NUL-containing html, overlong title or a missing or wildcard origin.
    """
    if (
        not parent_origin
        or "*" in parent_origin
        or not parent_origin.startswith(("http://", "https://"))
    ):
        raise ValueError("parent_origin must be an explicit http(s) origin")
    if not html.strip() or "\x00" in html:
        raise ValueError("html must be non-empty text")
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise ValueError(f"html exceeds {MAX_HTML_BYTES} bytes")
    if len(title) > MAX_TITLE_CHARS:
        raise ValueError(f"title exceeds {MAX_TITLE_CHARS} characters")
    # json.dumps yields a valid JS string literal; escape '<' so the value cannot close the script tag.
    target = json.dumps(parent_origin).replace("<", "\\u003c")
    bridge = _BRIDGE_JS.replace("__TARGET__", target)
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta name="description" content="{_html.escape(description, quote=True)}">'
        f"<title>{_html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body>{html}<script>{bridge}</script></body></html>"
    )

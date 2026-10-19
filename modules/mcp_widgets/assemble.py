"""Standalone HTML5 document assembly for the widget MCP server.

Ported from CopilotKit/OpenIntelligentUI@8391a94 apps/mcp (MIT). The theme/SVG/form CSS comes from design_css.py,
generated from the TypeScript design system (apps/web/src/modules/chat/generative-ui/design-system/index.ts)
by scripts/sync_design_css.py; a unit test fails when it is stale.
"""

import html as _html
import json

from modules.mcp_widgets.design_css import FORM_STYLES_CSS, SVG_CLASSES_CSS, THEME_CSS

MAX_HTML_BYTES = 256 * 1024
MAX_TITLE_CHARS = 200

# No remote origins at all: assembled documents are self-contained and cannot fetch or submit anywhere.
CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'"
)

_CSS = chr(10).join((THEME_CSS, SVG_CLASSES_CSS, FORM_STYLES_CSS))

# Bridge: widget -> embedding page. The target origin is injected as a JSON string literal; never '*'.
# Mirrors upstream renderer.ts (send-prompt/open-link/widget-resize, link interception, 200 ms height
# polling for 15 s) except that the target origin is explicit. `text` is the upstream key; `prompt` is kept
# for compatibility with earlier Umwelt-OS hosts.
_BRIDGE_JS = """
(function(){
  var TARGET=__TARGET__;
  function post(m){if(window.parent&&window.parent!==window)window.parent.postMessage(m,TARGET);}
  window.sendPrompt=function(t){t=typeof t==='string'?t:(t&&(t.text||t.prompt))||'';t=String(t).trim();if(t)post({type:'send-prompt',text:t,prompt:t});};
  window.openLink=function(u){post({type:'open-link',url:String(u)});};
  document.addEventListener('click',function(e){
    var a=e.target&&e.target.closest?e.target.closest('a[href]'):null;
    if(a&&a.href.startsWith('https://')){e.preventDefault();post({type:'open-link',url:a.href});}
  });
  var last=-1;
  function report(){
    var c=document.getElementById('content');
    var h=Math.ceil(c?c.offsetHeight:document.documentElement.scrollHeight);
    if(h!==last){last=h;post({type:'widget-resize',height:h});}
  }
  if(typeof ResizeObserver==='function')new ResizeObserver(report).observe(document.getElementById('content')||document.body);
  window.addEventListener('load',report);
  var iv=setInterval(report,200);
  setTimeout(function(){clearInterval(iv);},15000);
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
        f'<body><div id="content">{html}</div><script>{bridge}</script></body></html>'
    )

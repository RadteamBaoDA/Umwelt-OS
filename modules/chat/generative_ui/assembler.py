"""Assemble iframe-ready HTML documents with Umwelt-OS design system tokens and sandbox bridges."""

import re
from html import escape

APPROVED_CDN_HOSTS: frozenset[str] = frozenset({
    "cdnjs.cloudflare.com",
    "esm.sh",
    "cdn.jsdelivr.net",
    "unpkg.com",
})

UMWELT_THEME_CSS = """
:root {
  color-scheme: light;
  --bg: #f4f5f2;
  --surface: #ffffff;
  --text: #17211c;
  --muted: #69756e;
  --line: #dfe4df;
  --accent: #176a4a;
  --on-accent: #ffffff;
  --primary-foreground: #ffffff;
  --danger: #a82e35;
  --status-success: #176a4a;
  --status-warning: #a65b00;
  --chart-1: #176a4a;
  --chart-2: #2563eb;
  --chart-3: #d97706;
  --chart-4: #7c3aed;
  --chart-5: #0d9488;
  --color-background-primary: var(--surface);
  --color-background-secondary: var(--bg);
  --color-text-primary: var(--text);
  --color-text-secondary: var(--muted);
  --color-border-tertiary: var(--line);
}
html.dark, [data-theme="dark"] {
  color-scheme: dark;
  --bg: #141a17;
  --surface: #1d2621;
  --text: #edf3ee;
  --muted: #a1afa6;
  --line: #35433a;
  --accent: #77c69c;
  --on-accent: #102117;
  --primary-foreground: #102117;
  --danger: #ff9b9f;
  --status-success: #77c69c;
  --status-warning: #fbbf24;
  --chart-1: #77c69c;
  --chart-2: #60a5fa;
  --chart-3: #f59e0b;
  --chart-4: #a78bfa;
  --chart-5: #2dd4bf;
  --color-background-primary: var(--surface);
  --color-background-secondary: var(--bg);
  --color-text-primary: var(--text);
  --color-text-secondary: var(--muted);
  --color-border-tertiary: var(--line);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 12px;
  background: transparent;
  color: var(--text);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
  line-height: 1.5;
}
"""

SVG_CLASSES_CSS = """
.c-teal { fill: #E1F5EE; stroke: #0F6E56; color: #085041; }
.c-purple { fill: #EEEDFE; stroke: #534AB7; color: #3C3489; }
.c-coral { fill: #FAECE7; stroke: #993C1D; color: #712B13; }
.c-amber { fill: #FAEEDA; stroke: #854F0B; color: #633806; }
.c-blue { fill: #E6F1FB; stroke: #185FA5; color: #0C447C; }
.c-gray { fill: #F1EFE8; stroke: #5F5E5A; color: #444441; }
.c-red { fill: #FCEBEB; stroke: #A32D2D; color: #791F1F; }
.c-green { fill: #EAF3DE; stroke: #3B6D11; color: #27500A; }
.c-pink { fill: #FBEAF0; stroke: #993556; color: #72243E; }
html.dark .c-teal { fill: #085041; stroke: #5DCAA5; color: #9FE1CB; }
html.dark .c-purple { fill: #3C3489; stroke: #AFA9EC; color: #CECBF6; }
html.dark .c-coral { fill: #712B13; stroke: #F0997B; color: #F5C4B3; }
html.dark .c-amber { fill: #633806; stroke: #EF9F27; color: #FAC775; }
html.dark .c-blue { fill: #0C447C; stroke: #85B7EB; color: #B5D4F4; }
html.dark .c-gray { fill: #444441; stroke: #B4B2A9; color: #D3D1C7; }
html.dark .c-red { fill: #791F1F; stroke: #F09595; color: #F7C1C1; }
html.dark .c-green { fill: #27500A; stroke: #97C459; color: #C0DD97; }
html.dark .c-pink { fill: #72243E; stroke: #ED93B1; color: #F4C0D1; }
"""

FORM_STYLES_CSS = """
button, input, select, textarea {
  font: inherit;
}
button {
  min-height: 36px;
  padding: 6px 12px;
  border-radius: 8px;
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--text);
  cursor: pointer;
  font-weight: 600;
  transition: opacity 0.15s ease, background-color 0.15s ease;
}
button:hover:not(:disabled) {
  background: var(--bg);
}
button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
input, select, textarea {
  min-height: 36px;
  padding: 6px 10px;
  border-radius: 8px;
  border: 1px solid var(--line);
  background: var(--bg);
  color: var(--text);
}
"""

BRIDGE_JS = """
window.sendPrompt = function(payload) {
  var text = typeof payload === 'string' ? payload : (payload && payload.text ? String(payload.text) : '');
  if (!text) return Promise.resolve(false);
  window.parent.postMessage({ type: 'send-prompt', text: text }, '*');
  return Promise.resolve(true);
};
window.openLink = function(payload) {
  var url = typeof payload === 'string' ? payload : (payload && payload.url ? String(payload.url) : '');
  if (!url || !url.startsWith('https://')) return Promise.resolve(false);
  window.parent.postMessage({ type: 'open-link', url: url }, '*');
  return Promise.resolve(true);
};
window.Websandbox = window.Websandbox || {
  connection: {
    remote: {
      sendPrompt: function(opts) { return window.sendPrompt(opts); },
      openLink: function(opts) { return window.openLink(opts); }
    }
  }
};
window.createTripAnimator = window.createTripAnimator || function(options) {
  var opts = options || {};
  var stopCount = Math.max(1, Number(opts.stopCount) || 1);
  var pinElements = Array.isArray(opts.pinElements) ? opts.pinElements : [];
  var onFrame = typeof opts.onFrame === 'function' ? opts.onFrame : function() {};
  var onState = typeof opts.onState === 'function' ? opts.onState : function() {};
  var currentStop = 0;
  var state = 'paused';
  function renderStop(idx, progress) {
    currentStop = Math.max(0, Math.min(stopCount - 1, idx));
    pinElements.forEach(function(el, i) {
      if (!el || !el.style) return;
      el.style.opacity = i <= currentStop ? '1' : '0';
      el.style.transform = i <= currentStop ? 'translateY(0)' : 'translateY(-18px)';
    });
    onFrame({
      progress: progress,
      pinIndex: currentStop,
      pinProgress: 1,
      activeStop: currentStop
    });
  }
  renderStop(stopCount - 1, 1);
  return {
    play: function() { state = 'playing'; onState(state); renderStop(stopCount - 1, 1); state = 'complete'; onState(state); },
    pause: function() { state = 'paused'; onState(state); },
    replay: function() { state = 'playing'; onState(state); renderStop(0, 0); renderStop(stopCount - 1, 1); state = 'complete'; onState(state); },
    seek: function(index) { state = 'paused'; renderStop(Number(index) || 0, 1); onState(state); },
    dispose: function() { state = 'paused'; }
  };
};
document.addEventListener('click', function(e) {
  var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
  if (a && a.href && a.href.startsWith('https://')) {
    e.preventDefault();
    window.parent.postMessage({ type: 'open-link', url: a.href }, '*');
  }
});
function reportHeight() {
  var content = document.getElementById('content');
  var h = content ? content.offsetHeight : document.documentElement.scrollHeight;
  window.parent.postMessage({ type: 'widget-resize', height: h }, '*');
}
if (typeof ResizeObserver !== 'undefined') {
  var ro = new ResizeObserver(reportHeight);
  ro.observe(document.getElementById('content') || document.body);
}
window.addEventListener('load', reportHeight);
"""

_EXTERNAL_SCRIPT_RE = re.compile(
    r"<script\b[^>]*\bsrc\s*=\s*(['\"]?)(https?:)?//([^/'\"\s>]+)[^>]*>.*?</script\s*>",
    re.IGNORECASE | re.DOTALL,
)
_EXTERNAL_LINK_RE = re.compile(
    r"<link\b[^>]*\bhref\s*=\s*(['\"]?)(https?:)?//[^>]*>",
    re.IGNORECASE | re.DOTALL,
)
_EXTERNAL_IMG_RE = re.compile(
    r"<img\b([^>]*)\bsrc\s*=\s*(['\"]?)(https?:)?//([^/'\"\s>]+)([^>]*)>",
    re.IGNORECASE | re.DOTALL,
)


def _sanitize_markup(html: str, *, local_only: bool) -> str:
    """Strip unapproved external scripts, stylesheets, and remote tracking resources.

    When `local_only=True`, all external `<script src="http...">`, `<link href="http...">`,
    and remote `<img src="http...">` elements are stripped so the sandbox never initiates
    network egress. When `local_only=False`, only scripts from `APPROVED_CDN_HOSTS` over HTTPS
    are retained.
    """
    def _replace_script(match: re.Match[str]) -> str:
        if local_only:
            return "<!-- external script blocked by local_only privacy fence -->"
        scheme = (match.group(2) or "").lower()
        host = (match.group(3) or "").lower()
        if scheme == "https:" and host in APPROVED_CDN_HOSTS:
            return match.group(0)
        return "<!-- unapproved external script blocked -->"

    cleaned = _EXTERNAL_SCRIPT_RE.sub(_replace_script, html)
    cleaned = _EXTERNAL_LINK_RE.sub("<!-- external stylesheet blocked -->", cleaned)
    if local_only:
        cleaned = _EXTERNAL_IMG_RE.sub(
            "<!-- external image blocked by local_only privacy fence -->",
            cleaned,
        )
    return cleaned


def assemble_document(
    html: str,
    css: str = "",
    js_functions: str = "",
    js_expressions: str = "",
    *,
    title: str = "Umwelt-OS Widget",
    theme: str = "light",
    local_only: bool = True,
) -> str:
    """Assemble a complete iframe-ready HTML document with Umwelt-OS design tokens and bridges.

    Args:
        html: Body HTML fragment for the widget.
        css: Widget-specific CSS rules injected before the body markup.
        js_functions: Named JavaScript function declarations.
        js_expressions: Synchronous JavaScript invocation statements.
        title: Accessible document title.
        theme: Initial color scheme ('light' or 'dark').
        local_only: Whether to block all external network scripts, images, and connections.

    Returns:
        Complete HTML5 document string ready for an isolated `<iframe sandbox="allow-scripts">` srcdoc.
    """
    html_class = ' class="dark"' if theme == "dark" else ""
    sanitized_html = _sanitize_markup(html, local_only=local_only)
    if local_only:
        csp = (
            "default-src 'none'; "
            "script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; "
            "img-src data: blob:; "
            "font-src data:; "
            "connect-src 'none';"
        )
        importmap_block = ""
    else:
        csp = (
            "default-src 'self'; "
            "script-src 'unsafe-inline' 'unsafe-eval' "
            "https://cdnjs.cloudflare.com https://esm.sh https://cdn.jsdelivr.net https://unpkg.com; "
            "style-src 'unsafe-inline'; "
            "img-src 'self' data: blob: https://upload.wikimedia.org https://thumb.wikimedia.org "
            "https://basemap.nationalmap.gov; "
            "font-src 'self' data:; "
            "connect-src 'self' https://esm.sh https://cdn.jsdelivr.net;"
        )
        importmap_block = (
            '  <script type="importmap">\n'
            '  {"imports":{"three":"https://esm.sh/three@0.170.0",'
            '"three/examples/jsm/":"https://esm.sh/three@0.170.0/examples/jsm/",'
            '"gsap":"https://esm.sh/gsap@3.12.5",'
            '"d3":"https://esm.sh/d3@7.9.0",'
            '"chart.js":"https://esm.sh/chart.js@4.4.6/auto"}}\n'
            "  </script>\n"
        )

    user_js = "\n".join(part for part in (js_functions.strip(), js_expressions.strip()) if part)
    user_script_block = f"\n  <script>\n{user_js}\n  </script>" if user_js else ""

    return (
        f"<!DOCTYPE html>\n"
        f"<html{html_class}>\n"
        f"<head>\n"
        f'  <meta charset="utf-8">\n'
        f'  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"  <title>{escape(title)}</title>\n"
        f'  <meta http-equiv="Content-Security-Policy" content="{csp}">\n'
        f"{importmap_block}"
        f"  <style>\n"
        f"{UMWELT_THEME_CSS.strip()}\n"
        f"{SVG_CLASSES_CSS.strip()}\n"
        f"{FORM_STYLES_CSS.strip()}\n"
        f"{css.strip()}\n"
        f"  </style>\n"
        f"</head>\n"
        f"<body>\n"
        f'  <div id="content">\n'
        f"    {sanitized_html}\n"
        f"  </div>\n"
        f"  <script>\n"
        f"{BRIDGE_JS.strip()}\n"
        f"  </script>"
        f"{user_script_block}\n"
        f"</body>\n"
        f"</html>"
    )

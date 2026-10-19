# Widget MCP server

In-repo MCP server (port of CopilotKit/OpenIntelligentUI `apps/mcp`, MIT) in `modules/mcp_widgets/`. Off by default.

## Enable (HTTP)

```
MCP_WIDGETS_ENABLED=true
MCP_WIDGETS_TOKEN=<long random secret>        # required; startup fails if empty
MCP_WIDGETS_PARENT_ORIGIN=https://app.example # exact origin of the embedding page; empty = PUBLIC_ORIGIN
```

Streamable HTTP endpoint: `POST /mcp` or `/mcp/` (both served, no redirect; stateless, JSON responses) with `Authorization: Bearer <token>`.
Without or with a wrong token the response is 401; when disabled `/mcp` does not exist (404).
Use the trailing slash: `/mcp` is redirected to `/mcp/` by the router.

## stdio (desktop clients)

`python -m modules.mcp_widgets` (no token; the process boundary is the trust boundary).

## Surface

- Resources: `skills://list`, `skills://{name}` with `master-agent-playbook`, `svg-diagram-skill`, `agent-skills-vol2` (upstream names, allowlist only). The earlier names `master-playbook`, `svg-diagrams`, `advanced-visualization` still resolve as aliases but are not listed.
- Prompts: `create_widget`, `create_svg_diagram`, `create_visualization`.
- Tool `assemble_document(title, description, html)`: html <= 256 KB. Output is a standalone page with a CSP that allows no remote origins, theme/SVG/form CSS and a bridge posting `send-prompt` (`text`, plus `prompt` for compatibility), `open-link` (https link clicks are intercepted) and `widget-resize` (height polled every 200 ms for 15 s) to `window.parent` with the explicit configured origin (never `*`).

The CSS is generated into `modules/mcp_widgets/design_css.py` from `apps/web/src/modules/chat/generative-ui/design-system/index.ts` by `python scripts/sync_design_css.py`; a unit test fails when it is stale.

## Differences from upstream

- CSP: upstream allows `unsafe-eval` and cdnjs/esm.sh/jsdelivr/unpkg scripts and `'self'` connect. Umwelt-OS keeps a closed CSP with no remote origins (`default-src 'none'`, `connect-src 'none'`); widgets that need Chart.js, D3 or Three must inline their code.
- Transport: upstream uses a sessionful Hono transport with `ALLOWED_ORIGINS` defaulting to `*`; ours is stateless, bearer-protected, and CORS is opt-in with exact origins.
- Bridge: messages go to an explicit parent origin, never `*`.

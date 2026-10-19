# Widget MCP server

In-repo MCP server (port of CopilotKit/OpenIntelligentUI `apps/mcp`, MIT) in `modules/mcp_widgets/`. Off by default.

## Enable (HTTP)

```
MCP_WIDGETS_ENABLED=true
MCP_WIDGETS_TOKEN=<long random secret>        # required; startup fails if empty
MCP_WIDGETS_PARENT_ORIGIN=https://app.example # exact origin of the embedding page; empty = PUBLIC_ORIGIN
```

Streamable HTTP endpoint: `POST /mcp/` (stateless, JSON responses) with `Authorization: Bearer <token>`.
Without or with a wrong token the response is 401; when disabled `/mcp` does not exist (404).
Use the trailing slash: `/mcp` is redirected to `/mcp/` by the router.

## stdio (desktop clients)

`python -m modules.mcp_widgets` (no token; the process boundary is the trust boundary).

## Surface

- Resources: `skills://list`, `skills://{name}` with `master-playbook`, `svg-diagrams`, `advanced-visualization` (allowlist only).
- Prompts: `create_widget`, `create_svg_diagram`, `create_visualization`.
- Tool `assemble_document(title, description, html)`: html <= 256 KB. Output is a standalone page with a CSP that allows no remote origins, theme/SVG/form CSS and a bridge posting `send-prompt`, `open-link`, `widget-resize` to `window.parent` with the explicit configured origin (never `*`).

The CSS in `assemble.py` is a minimal copy of `apps/web/src/modules/chat/generative-ui/design-system/index.ts`; keep token values in sync.

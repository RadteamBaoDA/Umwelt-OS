# AnythingLLM Chat Source Port (R07 / P06-T3)

## Provenance

This is a small source-slice adaptation from Mintplex Labs' AnythingLLM repository, not an AnythingLLM application deployment or a claim of feature parity.

- Repository: <https://github.com/Mintplex-Labs/anything-llm>
- Reviewed immutable upstream revision: `128a01575a50f0284aeca75a93399b6fb1db0328` (2026-09-25 snapshot).
- Root license: MIT, Copyright (c) Mintplex Labs Inc.; the complete notice is in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
- Upstream tree was a clean detached checkout at that revision. Original blob IDs are in the table below; the ignored source-region ledger records line/symbol detail, retained logic, local file SHA-256, and review state.
- The port uses only the listed upstream frontend source regions. It does not include upstream CSS/assets, provider/model code, API transport, auth, workspace state, storage, tool surfaces, or delete-tail edit behavior.

## Retained slices and local changes

| Upstream source and original blob | Local adaptation | Retained behavior | Local changes and exclusions |
| --- | --- | --- | --- |
| `frontend/src/hooks/useAutoScroll.js` — `23d79995d187c64bbeba34937cf16b0dbaa408d8` | `apps/web/src/modules/chat/use-chat-scroll.ts` | Follow-bottom state, scroll direction, wheel/touch opt-out, bottom detection, bounded layout pinning. | Typed refs/events; conversation reset; active streaming input comes from the existing transcript; cancels pin and stream animation frames; honors reduced-motion preference; omits upstream Appearance storage and imperative-ref API. |
| `frontend/src/components/WorkspaceChat/ChatContainer/PromptInput/index.jsx` — `42ee50c71e671c012ea627864036332631eb7265` | `apps/web/src/modules/chat/chat-composer.tsx` | Enter submits, Shift+Enter inserts a newline, textarea expands with content. | `ChatController` remains the sole in-memory draft owner; composition events do not submit; existing Send/Stop and shadcn controls remain. Upstream storage, undo/redo, debounce, browser event bus, agent/tool/attachment/audio controls, router, and provider behavior are omitted. |
| `frontend/src/utils/chat/markdown.js` — `44c6e0034768368e4dad32a0e2d43423f26bcc45` | `apps/web/src/modules/chat/chat-markdown.tsx` | markdown-it rendering, fenced/code-block handling, link presentation. | Raw HTML is disabled, URLs are protocol-checked, external images are rendered as alt text without fetching, code is escaped, and generated HTML is DOMPurify-sanitized in a browser effect. Appearance/theme globals, UUID/copy delegation, thought rendering, KaTeX, highlighters, fonts and upstream CSS are omitted. |
| `frontend/src/utils/chat/purify.js` — `a6cf85206602c4153ff4c117c3177f4764ff8053` | `apps/web/src/modules/chat/chat-markdown.tsx` | DOMPurify as the post-render sanitization boundary. | Browser window access occurs only in the effect; a restricted allowlist blocks raw/active elements and data attributes. The upstream module-time `window` initialization is not retained. |
| `frontend/src/hooks/useCopyText.js` — `526fcdbccd86e9c5b88d2e93c643b205775969f7` | `apps/web/src/modules/chat/use-copy-message.ts` | Per-message copied-state feedback with timeout. | Copies plain text only; feedback/timers clear on message action, conversation change, and unmount; clipboard failures are surfaced. Upstream rich-HTML copy and thought-content parsing are omitted. |

The exact local source line ranges and SHA-256 digests must be read from the frozen implementation. They are recorded in `.superpowers/sdd/r07/source-regions.md`; that ignored evidence file is not a substitute for this tracked provenance summary.

## Umwelt-OS contracts preserved

- The existing root `ChatController`, shared `ChatSession`, TanStack Query transcript, owner-authenticated API and SSE response-run stream remain authoritative. The drawer retains only New chat, messages, composer/Send/Stop and close; history and edit/regenerate management stay in full Chat.
- No prompt, provider credential, API key, or upstream event bus is introduced into the browser. Drafts and unresolved mutation retry envelopes are in memory only.
- Edits and regenerations append linked user/assistant entries. They retain earlier prompts, answers and citations, bind to the original run's captured retrieval context, reject stale selected evidence at worker fences, and use durable per-conversation request receipts. No old row is overwritten or deleted.
- Citation navigation carries the source citation's exact document-version and chunk IDs to the Documents-owned owner-authenticated reader. Version IDs select content and never grant access or silently resolve to a newer version.

## Locked dependencies and notices

- `markdown-it` 15.0.2 (MIT), direct dependency in `apps/web/package.json` and `package-lock.json`; notice is reproduced in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
- `dompurify` 3.4.16 (package license expression `MPL-2.0 OR Apache-2.0`); Apache-2.0 notice is reproduced in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
- No `he`, `highlight.js`, KaTeX, fetch-event-source, upstream assets, or other AnythingLLM runtime packages were added for this port.

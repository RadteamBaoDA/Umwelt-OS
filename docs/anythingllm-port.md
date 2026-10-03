# AnythingLLM Chat Port Documentation (R07 / P06-T3)

## Overview

In accordance with Phase 6 and specification reconciliation task R07, the chat interface adapts AnythingLLM's interaction patterns into BBD-OS's privacy-first, single-owner modular monolith architecture. Rather than deploying an external AnythingLLM instance or embedding an iframe, the essential conversational workflows (persistent thread management, grounded retrieval evidence presentation, SSE streaming, and in-memory draft retention) are cleanly ported and implemented as repository-owned Next.js components.

## Architecture and Adaptation Boundaries

### 1. Unified ChatSession Component
- A single component (`apps/web/src/modules/chat/chat-session.tsx`) powers both the quick-chat drawer and the full `/chat` route.
- Thread selection and server transcript remain canonical TanStack Query state (`['chat', 'conversations']` and `['chat', 'conversations', id]`).
- Server-side responses continue running independently of whether the browser drawer remains open or closed.

### 2. Quick-Chat Sheet Drawer vs. Full Chat Route
- **Quick-Chat Drawer (`chat-drawer.tsx`)**:
  - Accessible right-side overlay implemented via Radix Dialog / shadcn Sheet (`SheetContent`).
  - Closed by default, responsive width on desktop (`sm:max-w-xl md:max-w-2xl lg:max-w-3xl xl:max-w-4xl`), full viewport width on mobile.
  - Minimal presentation containing only: New chat action, conversation messages, composer with Send / Stop, and close.
  - When closed, underlying page gets 100% width with no reserved blank column.
  - Pressing Escape or clicking close restores focus to the triggering element and closes presentation only, without aborting in-flight generation or losing composer drafts.
  - Provides a direct link to transition seamlessly to the full `/chat` route.
- **Full Chat Route (`/chat/page.tsx`)**:
  - Split layout with conversation history sidebar (`chat-history.tsx`), thread search, deletion confirmation, and active `ChatSession`.
  - Responsive collapse for mobile viewports.

### 3. Composer and Ephemeral Draft Preservation
- `chat-composer.tsx` provides multi-line textarea input, Enter-to-send, and Shift+Enter for newlines.
- Provides a separate, explicit **Stop** button during streaming generation to trigger cancellation (`POST /api/v1/responses/{id}/cancel`).
- In-memory draft state is preserved within `ChatController` across drawer open/close cycles.
- **Privacy invariant**: Private draft content is strictly kept in memory and is **never** written to `localStorage` or `sessionStorage`.

### 4. Grounded Citation Evidence Inspector
- `citation-panel.tsx` renders structured citations pointing to verified document versions and chunks.
- Each citation exposes the title, document version, quote excerpt, observed timestamp, and direct links to the document revision in Knowledge (`/knowledge/documents/{id}`).

### 5. Transport and Provider Isolation
- Chat routes and background workers communicate solely through BBD-OS's internal API (`/api/v1/conversations`, `/api/v1/conversations/{id}/messages`, `/api/v1/responses/{id}/events`).
- Streaming is delivered via Server-Sent Events (SSE) with `Last-Event-ID` resumption support.
- Provider egress is completely mediated by `ModelGateway` under owner privacy policy; no client-side provider secrets or external third-party AI scripts are used.

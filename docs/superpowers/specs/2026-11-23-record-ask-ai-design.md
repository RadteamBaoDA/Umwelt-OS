# Per-record Ask AI with workspace-wide follow-up — design

Date: 2026-11-23 · Status: approved by owner 2026-11-23 · Branch: `feature/translate-news`

## Intent

The owner reads news and other document-backed records on the dashboard and wants one **Ask AI** button on
each record. Clicking it opens chat with that record attached. From there the owner's own prompt decides
the task: summarize it, ask about it, or find related data anywhere in the workspace ("tìm tin liên quan").
The record is the anchor, not a fence.

## Goals

- One Ask AI button on every dashboard row that is backed by a document version (has `sourceId`,
  `documentId` = item id, `documentVersionId`).
- The chat opened by that button always has the record's evidence in context, and can also use normal
  workspace retrieval (hybrid search, entity context, temporal context, reranking, follow-up query rewrite)
  across all permitted sources.

## Non-goals

- No quick-prompt chips, no second "find related" button, no in-chat mode toggle. The prompt decides.
- No Ask AI for non-document records (weather, metrics/finance, maps, live map, GitHub, goals, tasks,
  timeline, observations). They have no document identity; a new context kind would be needed.
- No change to multi-select **Ask about N items** (bulk selection): it stays exact (`selected_only`).
- No change to the gadget-header "Ask with Umwelt" (`kind: 'gadget'`) action.

## Anti-goals

- The record silently missing from the answer's evidence (dropped by search ranking or context budget).
- Weakening privacy: a `local_only` record must still be refused before a run
  (`reject_unsendable_selection`, 409 `selection_local_only`); stale or out-of-scope versions must still be
  refused (409 `selection_unavailable`).
- Letting a client turn on workspace search for an exact selection of several items.

## Current state (verified)

- Frontend single-row Ask AI exists in feed rows (`gadgets/feed-parts.tsx` `FeedItemActions`; non-Telegram
  feeds gated on `ctx.isOwner`) and video rows (`gadgets/video-panel.tsx`). Media (`media-parts.tsx`) and
  table (`table-panel.tsx`) offer only bulk "Ask about N". `job-list.tsx`, `intelligence-panel.tsx`,
  `text-panel.tsx`, `watchlist-gadget.tsx` render document-backed rows with no Ask AI. All callers use
  `openDrawer({ context: { kind: 'selection', items } })` from `useChatController`.
- Backend `resolve_gadget_context` (`modules/chat/_public/conversations.py:307`) resolves each item through
  Documents, builds `selected_refs`, `selection_fences`, a `source_scope` limited to the items' sources, and
  always sets `selected_only: True`.
- `selected_only` in `modules/chat/retrieval.py` skips search (l.834), entity and temporal context, reranking
  and current-version relaxation; `_worker/setup.py` also skips the follow-up query rewrite. Pinned refs are
  added with `collector.add_default(ref, 1.0)` after search refs; search scores are RRF (~0.033 or less), so
  they already sort first. The real risks are `_read_evidence_items` keeping only the first 100 refs in
  collector order (l.716), the 64 KB byte budget, and rerank reordering.
- `_resolve_selected_chunk_ids` raises 422 `selection_too_large` for a long document when no `chunkId` is
  given; dashboard rows never send one.
- The selection context lives in client memory (`chat-controller.tsx`), is resent on every send
  (`use-chat-send.ts`) and stored per run (`ResponseRun.retrieval_context`); the conversation row stores only
  `context_kind`/`context_resource_id`. `chat-attachments.tsx` (`withAttachments`) and `chat-context-bar.tsx`
  rebuild `{ kind: 'selection', items }` without extra keys.
- Watchlist API returns `source_id`/`document_id`/`document_version_id`, but `WatchlistItem` in
  `watchlist-gadget.tsx` drops them. `intelligence-panel.tsx` rows are correlation groups (lists of document
  ids), not one record.
- Spec §4634: selecting items supports Ask AI and keeps "selected source identities/versions as thread
  context" with citations reopening the exact item. Still satisfied by the anchor mode.

## Approaches considered

- **A — one button, record as anchor + workspace retrieval (chosen).** Small backend mode on the existing
  `selection` context, frontend coverage on all document rows. One chat covers summarize and "related".
- B — two buttons (exact "ask about this" + "find related"). Clear semantics, but crowds the already busy
  hover row and makes the owner choose before asking. Rejected by the owner.
- C — no backend: prefill plain chat with title/URL text. Cheapest, but the record may not be retrieved
  (unreliable summaries) and loses version/scope/local-only checks. Rejected.

## Design

### Contract

- Request context gains an optional flag on the existing kind:
  `{ kind: 'selection', items: [one item], anchor: true }`.
- Server accepts `anchor: true` only with exactly one item; otherwise 422 (`selection_detail` code
  `anchor_requires_single_item`). The flag is a request for less restriction, so it is honoured only after
  the same Documents resolution, fence validation and local-only refusal as today.

### Backend (`modules/chat`)

- `resolve_gadget_context`: same per-item resolution, refs and fences. When `anchor` is true, return
  `selected_refs` and `selection_fences` (fences still feed `reject_unsendable_selection`), set
  `selected_only: False`, `source_scope: []` (no filter, plain-chat meaning), and a server-derived
  `anchored: True` marker. Strip client-sent `anchored` like `selected_only` today.
- Long records *(design call)*: in anchor mode the resolver does not raise 422 for a truncated document; it
  takes the document's leading chunks up to the anchor cap below. Exact selection keeps the 422.
- Retrieval with `anchored`: run the normal (non-`selected_only`) path, and guarantee the anchor's refs:
  collect them **before** search refs so the 100-ref cut never drops them, keep them first through
  `_fit_context_budget` and reranking (rerank applies to search refs only, anchor stays on top).
- Anchor cap *(design call)*: the anchor may use at most **half the byte budget** and at most **20 chunks**
  (leading chunks first); the rest is left for workspace results, so "find related" always has room.
- Version *(design call)*: anchor refs are read like other non-exact refs (`require_current_version=False`,
  no fences at read or stream revalidation). If the record got a new version between click and run, the
  answer cites the version that was attached. No mixed fenced/unfenced read.
- Follow-ups: the client resends the anchor context with every send, so follow-ups in the open drawer keep
  it and get the normal query rewrite. After a page reload or reopening the thread from history the anchor
  is gone, same as today's selection context *(accepted, no server persistence)*.
- Tools and task-draft offers key off `selected_only`, so anchored turns get them like plain chat
  *(intended)*.
- Builder notes: add the anchored marker to `_TASK_THREAD_DROPPED_KEYS` (`_worker/base.py`) and strip it in
  the non-selection branch of `resolve_gadget_context`, like `selected_only`.

### Frontend (`apps/web`)

- One shared helper builds the anchor context from `{ sourceId, documentId, documentVersionId }` and opens
  the drawer; every single-row Ask AI uses it. Existing feed and video single-row buttons switch to it.
- Add the row button, in each gadget's existing hover/actions pattern, to media rows, table rows, job-list,
  text-panel and watchlist rows that carry a document identity. Watchlist: carry the three ids through
  `WatchlistItem` first.
- Out of scope *(design call)*: `intelligence-panel.tsx` (groups, not one record) and the News page
  `story-detail.tsx` Ask AI (multi-chunk, stays exact).
- Editing the chat context *(design call)*: when the owner attaches a file or edits the context chips after
  Ask AI, the rebuilt context has no `anchor` and becomes a normal exact selection, as today. No new 422 path
  from the client.
- Gating unchanged per gadget *(design call, no permission change)*: feed keeps its Telegram exception;
  every other gadget, Telegram media included, stays owner-only.
- Reuse the existing `askAbout.message` label; no new strings beyond any test ids.

### Tests

- Backend: anchor resolution (flag honoured for one item, 422 for several, `local_only` still 409, stale
  still 409, long document gives leading chunks not 422, client-sent `anchored`/`selected_only` stripped);
  retrieval keeps anchor refs first, within the cap, and leaves room for search refs when >100 refs or the
  byte budget would otherwise cut them.
- Frontend: each covered gadget renders the button and calls `openDrawer` with `anchor: true` and one item;
  bulk selection still sends no `anchor`.

## Left to the builder

- Exact name and placement of the anchor marker inside the resolved context dict and `AnswerContextRequest`.
- How anchor priority is enforced in `_RefCollector`/budget/rerank (score, separate list, or pin flag), as
  long as the anchor-first, never-dropped rule holds.
- Per-gadget button placement within each gadget's existing action pattern (design system rules apply).

# Workspace access helpers (`_actor` / `_admit` consolidation)

Canonical implementations live in `core/workspaces/admission.py`. The pre-existing
`core/workspaces/access.py` owns lock/CAS primitives and is imported by
`core.workspaces.public`, so the new helpers sit beside it to avoid an import cycle.
Module copies are **not** replaced by this work package; each module package adopts
the helpers using the mapping below. Fence primitives are resolved through
`core.workspaces.public` at call time, so existing monkeypatches keep working.

## `_actor(scope) -> int` (21 copies) -> `admission.actor_id`

| Variant | Behaviour | Copies | Maps to |
| --- | --- | --- | --- |
| A | `actor_user_id if InternalJobScope else user_id` | goals/seed.py:30, tasks/seed.py:29, search/indexing.py:52, notifications/public.py:39, tools/browser.py:16, tools/browser_public.py:29, tools/mcp_repository.py:61, memory/public.py:74, automations/scope.py:14, knowledge/relationships/public.py:70, knowledge/observations/public.py:53, knowledge/entities/public.py:82, knowledge/entities/corrections.py:47, knowledge/documents/public.py:2075 | `actor_id` |
| B | `user_id if (WorkspaceContext, AdminWorkspaceReadContext) else actor_user_id` | goals/public.py:59, tasks/public.py:60, timeline/public.py:62, news/topics.py:50, dashboard/public.py:126, dashboard/briefs.py:55 | `actor_id` (identical for the three `Scope` members) |
| C | **DIFFERS**: `user_id if WorkspaceContext else actor_user_id` | knowledge/temporal/public.py:43 | keep own logic: an `AdminWorkspaceReadContext` yields the *operator* (`actor_user_id`); the canonical helper yields the data owner |

## `_admit(...)` (24 definitions) -> `admission.admit_scope`

Options: `allowed_scopes`/`scope_label` (TypeError text), `owner_only`,
`admin_read_only_on_lock`, `check_flag`.

| Variant | Copies | Mapping |
| --- | --- | --- |
| Owner/Job only, member 403, flag check | notifications/public.py:50, tools/browser_public.py:40, automations/scope.py:25 | `allowed_scopes=(WorkspaceContext, InternalJobScope)` |
| Owner, admin read-only on lock, flag check | news/topics.py:55, dashboard/public.py:131, dashboard/briefs.py:60 | `admin_read_only_on_lock=True` |
| Typed scope + admin read-only | goals/public.py:64 ("goal"), tasks/public.py:65 ("task"), timeline/public.py:67 ("timeline"), knowledge/entities/public.py:87 ("entity") | `allowed_scopes=ALL_SCOPE_TYPES, scope_label=..., admin_read_only_on_lock=True` |
| Typed (Workspace/Job) without admin check | memory/public.py:84 ("Memory workspace"), knowledge/entities/corrections.py:52 ("correction workspace") | `allowed_scopes=(WorkspaceContext, InternalJobScope), scope_label=...` |
| Owner, no `expected` parameter | knowledge/observations/public.py:58 | plain `admit_scope` (`expected=None` is the lock default) |
| Owner, no flag validation, read only | knowledge/temporal/public.py:48 | `check_flag=False` |
| Search public with `allow_member` | search/public.py:123 | `owner_only=not allow_member, check_flag=False` (`is_member_scope` is a member `WorkspaceContext`) |
| News stories (members allowed) | news/stories.py:47 | `owner_only=False` |
| **Order differs**: flag check precedes admin-lock denial | knowledge/relationships/public.py:75 | maps except error precedence when both a bad flag and an admin lock are supplied; keep own copy unless that is accepted |
| **DIFFERS**: `expected` compared after a non-locking read (409 "Workspace access changed") | search/indexing.py:57 | keep own logic |
| **DIFFERS**: request-bound, `auth_sessions=(authenticated_session_ref(request),)` | connectors/oauth_routes.py:173, connectors/reddit_oauth_routes.py:250 | keep own logic |
| **DIFFERS**: request-bound, `lock=True` default, `_flag(request)` | connectors/github/routes.py:71 | keep own logic |
| **DIFFERS**: compares a stored `_Admission` fence, 409 "Workspace authority changed" | knowledge/temporal/worker.py:97 | keep own logic |
| **Unrelated**: `McpRuntime._admit` method delegating to `mcp_repository.admit` | tools/mcp_runtime.py:129 | keep own logic |
| **Unrelated**: provider rate-limit pacing | dashboard/live_map_routes.py:193 | keep own logic |

## `_require_owner(scope)` (5 copies)

* tools/browser_public.py:33, tools/mcp_repository.py:65, notifications/public.py:43,
  automations/scope.py:18: type check plus member 403; `admit_scope` performs both checks.
* news/routes.py:49 **DIFFERS** (admits `admin_viewer` via a role-string check): keep own logic.

## Adoption status: misc-modules package

`admit_scope` raises `core.errors.ForbiddenError` (same 403 status and detail as before) so domain
code stays framework-free; callers that handle both error families use `core.errors.is_http_error`.

Adopted (thin `_admit` wrappers keep the module-private name; `_actor` is the imported `actor_id`):
goals/public, tasks/public (typed scopes, labels `goal workspace` / `task workspace`), dashboard/public,
dashboard/briefs, notifications/public (workspace or job scopes), search/public (`allow_member` variant).
`actor_id` only: goals/seed, tasks/seed, search/indexing. Left as documented divergent copies:
search/indexing `_admit` (non-locking compare, 409), dashboard/live_map_routes pacing helper.

Export helpers: `core/export_pages.py` holds the snapshot cursor, byte-bound and digest code shared by
goals, tasks and dashboard exports; the remaining `export_page`/`validate_export_fences` copies are
listed in that module's docstring for later adoption.


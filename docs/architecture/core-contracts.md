# Core contracts: domain errors, module descriptors, erasure registry

Scope: remediation plan sections 3.7 (R4 DIP/OCP) and 3.5 (R2). Module packages migrate onto
these contracts later; nothing here changes API behaviour.

## Domain errors (`core/errors.py`)

`DomainError(Exception)` carries `status_code`, `detail` and `headers` without importing
FastAPI/Starlette. Subclasses cover the codes actually raised by non-route code
(`HTTPException(status_code=...)` counts: 409 x505, 404 x253, 422 x188, 403 x128, 503 x102,
401 x64, 413 x29, 400 x28, 429 x10, 410 x8, 502 x7, 415 x4, 504 x3, 428 x1, 500 x1):
`BadRequestError`, `UnauthorizedError`, `ForbiddenError`, `NotFoundError`, `ConflictError`,
`GoneError`, `PayloadTooLargeError`, `UnsupportedMediaTypeError`, `ValidationFailedError`,
`PreconditionRequiredError`, `RateLimitedError`, `BadGatewayError`, `UnavailableError`,
`GatewayTimeoutError`. A code without a subclass uses `DomainError(detail, status_code=...)`.

`install_error_handling` registers a `DomainError` handler that calls the same
`render_http_error` as the `HTTPException` handler, so status, headers and the
`{"error": {code, message, details, requestId}}` envelope are identical
(`tests/unit/core/test_domain_errors.py` compares bytes). Worker code that must treat both
families uniformly uses `error_status(exc) -> int | None` (also `error_detail`, `is_http_error`)
instead of `except HTTPException`. `DomainError` deliberately does not subclass
`HTTPException`, so existing `except HTTPException` blocks do not catch it: migrate a module's
raise sites and its `except` clauses together.

## Module descriptors (`core/module_descriptor.py`, `core/modules.py`)

All `modules/**/descriptor.py` classes now derive from the frozen `ModuleDescriptor` dataclass
(old class names and `descriptor` exports unchanged). New fields:

* `routers: tuple[RouterSpec, ...]`: lazily imported `"package.module[:attr]"` targets with a
  global mount `order`. `apps/api/main.py` mounts `CORE_ROUTERS` + descriptor routers sorted by
  `order`; the baseline `tests/fixtures/api_route_table.json` (guarded by
  `tests/unit/core/test_api_route_order.py`) proves the route table is identical.
* `erasure_module` / `erasure_order`: account-erasure package and its position.

Lifecycle registry (`register_modules()`, 22 capability modules) is unchanged and typed as
`ModuleRegistry = dict[str, ModuleDescriptor]`. New `settings`, `translations`, `pick`,
`model_gateway`, `mcp_widgets` descriptors are *platform* descriptors
(`PLATFORM_DESCRIPTORS`): they contribute routers/erasure but are intentionally not in the
lifecycle registry, so the module-toggle UI, worker gates and tool registry are unchanged.
`mcp_widgets` declares no routers because it is an optional ASGI mount (`/mcp`) created by
`_mount_widgets_server` when `mcp_widgets_enabled`.

Router order is explicit because first-match-wins route resolution is order dependent
(for example `modules.tools.browser_control` mounts between connectors operator routes and
connector builtins). Orders are spaced by 10 and uniqueness is enforced.

`create_app` was split into `_validated_csrf_secret`, `_make_lifespan`, `_new_app`,
`_include_descriptor_routers`, `_register_native_tools`, `_compose_tools_module`,
`_mount_widgets_server`; middleware registration order is preserved.

`apps/worker`'s `cron_jobs` are defined in the worker entrypoint, not per module; descriptors
keep `scheduled_jobs` (job-name ownership) as the existing source of truth.

## Erasure inversion (`core/auth/deletion_cleanup.py`)

The 23 hard-coded `modules.*.erasure` imports are replaced by `erasure_descriptors()`
(ascending `erasure_order`, unique). The order is explicit per descriptor and
`tests/unit/core/test_module_descriptors.py` asserts it equals the old tuple. The packages are
still imported when `deletion_cleanup` is imported, preserving the metadata-registration side
effect that account recovery relies on. Stage hooks (`connectors`, `documents`, `settings`,
`temporal`, `tools`, `translations`) are looked up by module ID.

### Remaining core -> modules imports

Inverted through `core/capabilities.py` (descriptor `capabilities`, resolved lazily; no import edge from core): setup
display name (`settings`), module availability for the tool registry (`settings`), agent checkpoint erasure (`agents`),
document and brief access projections (`documents`, `dashboard`) and translation purge (`translations`).

Still imported (allow-listed in `.importlinter`, contracts `core-does-not-import-modules` and `layers`):

* `core/modules.py`: imports each `modules.*.descriptor` by design (the composition root).
* `modules/sources/system_routes.py` (Sources-owned, mounted under `/api/v1/system` at order 55): the purge-operation
  read; core no longer imports `modules.sources`.

See `docs/architecture/module-boundaries.md` for the module-to-module rules.

## Descriptor dependency audit

`tests/unit/core/test_descriptor_dependencies.py` AST-scans each lifecycle module's imports and requires
`dependencies | optional_dependencies` to match them exactly (`MAX_UNDECLARED = MAX_UNUSED = 0`, empty allow-lists).
Only `dependencies` cascade in `effective_modules` (`tests/unit/core/test_effective_modules.py`).

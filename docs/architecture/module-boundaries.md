# Module boundaries

Scope: remediation plan section 3.7 ("Module boundaries"). Modules integrate only through each other's public contracts.
`lint-imports` (`.importlinter`, wired into `make lint` and `./scripts/dev.ps1 lint`) and
`tests/unit/architecture/test_module_boundaries.py` enforce the rules below.

## Rules

1. A module package (a directory with a `descriptor.py`, plus the `modules.knowledge` aggregator) may import another
   module package only through its `public` or `descriptor` submodule. `models`, `schemas`, `worker`, `routes` and
   helper modules are private to the owner. Function-local and `TYPE_CHECKING` imports count.
2. Cross-module DTOs are re-exported from the owner's `public.py` (plain imports listed in `__all__`). The `schemas`
   module stays the definition site; importers use `from modules.<owner>.public import ...`.
3. Cross-module persistence reads use an **approved read projection** exported by the owner's `public.py`:

   | Projection (alias of the owner's model) | Owner | Readers |
   | --- | --- | --- |
   | `SourceReadProjection` | sources | knowledge.documents, search, news, chat (db_query), connectors |
   | `DocumentReadProjection`, `DocumentChunkReadProjection`, `DocumentVersionReadProjection`, `NormalizedVersionProvenanceReadProjection` | knowledge.documents | search, news, chat (db_query) |
   | `ObservationReadProjection` | knowledge.observations | chat (db_query) |
   | `SourceIngestionStateReadProjection` | ingestion | connectors (scheduler) |
   | `ConnectorOAuthGrantReadProjection`, `ConnectorProvisioningReadProjection` | connectors | ingestion (native verifiers) |

   Readers build the same `SELECT`/`JOIN` statements as before (SQL is unchanged); permission and deletion checks stay
   with the caller's own admission fence plus the owner's workspace/source predicates. A projection is read-only by
   contract: the guard test fails when a caller instantiates it or passes it to `insert/update/delete/add/merge`.
   Writes that touch another module's table go through an owner function instead (for example Translations saves its
   settings through `modules.settings.public.save_translation_settings_cas`).
4. `core` never imports `modules` (see `core/capabilities.py`): a module declares a `CapabilitySpec` in its descriptor and
   core calls the typed wrapper, which resolves the owner's public attribute lazily at call time.
5. `apps` is the composition root and may import module routers/workers.

## Import cycles

Public facades import their implementation eagerly, so a DTO-level cycle between two owners used to surface as
`ImportError` when the facade became the import path. Techniques used (in order of preference):

* move the cycle-closing import to call time (function-local) when it is only used inside a function
  (`sources.schemas` validator, `documents._documents`);
* annotation-only use becomes a `TYPE_CHECKING` import with `from __future__ import annotations`
  (`ingestion._batches/_collection/_native_collection`, `connectors.provider_options/selection`);
* hoist an owner's own DTO import above the heavy implementation imports (`ingestion.public`, `knowledge.documents.public`);
* lazy PEP 562 re-exports in the facade (`connectors.public`, `automations/goals/tasks/agents.public`), with the
  mapping and `__getattr__` defined before the first import so a partially initialised facade still resolves them;
* `modules/connectors/service/__init__.py` loads the facade first so direct imports of a `service.*` module work.

Remaining at package level (not broken, needs shared DTO relocation or events): one strongly connected component of
21 module packages (35 mutually importing pairs). The only documented private import left in `modules/` is
`ingestion.schemas -> connectors.github.schemas` (dependency-free DTO leaf, listed in `.importlinter`).

## Descriptor dependencies

`ModuleDescriptor.dependencies` are hard lifecycle gates (`core.modules.effective_modules` disables a module when any
is unavailable). `optional_dependencies` record import coupling without a gate; declaring every import as a hard
dependency would put 21 packages in one cycle, so disabling any of them would disable all. The descriptor audit test
requires `dependencies | optional_dependencies` to equal the real import set (zero allow-list).

"""Custom import-linter contract: modules integrate only through each other's public contracts.

The ``module_public_api`` contract enforces the modular-monolith rule from AGENTS.md ("Keep module internals private;
integrate through public contracts or events"): a module package may import another module package only through the
submodules named in ``allowed_submodules`` (``public`` and ``descriptor``). Persistence models, schemas, workers, routes
and helpers are private to their owner; cross-module DTOs and approved read projections are re-exported from the
owner's ``public.py``.

A *module package* is any package under the configured root that contains a ``descriptor`` submodule, plus every
package listed in ``extra_packages`` (aggregators such as ``modules.knowledge`` that own a ``public`` facade but no
descriptor). Imports inside one module package (including its nested subpackages) are never restricted, and imports of
a module package itself (``import modules.chat``) are allowed. Function-local and ``TYPE_CHECKING`` imports are
imports like any other: the graph is built from the AST.

Documented exceptions go in ``ignore_imports`` with a justification; an exception that no longer matches an import is
reported as an error so stale entries cannot linger.
"""

from __future__ import annotations

from typing import cast

from grimp import ImportGraph
from importlinter.application import contract_utils, output
from importlinter.application.contract_utils import AlertLevel
from importlinter.domain import fields
from importlinter.domain.contract import Contract, ContractCheck


class ModulePublicApiContract(Contract):
    """Forbid imports of another module package's non-public submodules.

    Configuration options:
        - package:            Root package holding the module packages (for example ``modules``).
        - allowed_submodules: First-level submodule names importable from other module packages.
        - extra_packages:     Additional packages treated as module packages although they have no ``descriptor``.
        - ignore_imports:     Documented exceptions (``importer -> imported`` expressions).
        - unmatched_ignore_imports_alerting: ``error`` (default), ``warn`` or ``none`` for stale exceptions.
    """

    type_name = "module_public_api"

    package = fields.StringField()
    allowed_submodules = fields.SetField(subfield=fields.StringField())
    extra_packages = fields.SetField(subfield=fields.StringField(), required=False)
    ignore_imports = fields.SetField(subfield=fields.ImportExpressionField(), required=False)
    unmatched_ignore_imports_alerting = fields.EnumField(AlertLevel, default=AlertLevel.ERROR)

    def check(self, graph: ImportGraph, verbose: bool) -> ContractCheck:
        """Return a ``ContractCheck`` listing every import that reaches into a foreign non-public submodule.

        Reads only the (copied) import graph; the ignored imports are removed from that copy before the scan.
        """
        removal = contract_utils.remove_ignored_imports_and_report(
            graph=graph,
            ignore_imports=self.ignore_imports,  # type: ignore[arg-type]
            unmatched_alerting=self.unmatched_ignore_imports_alerting,  # type: ignore[arg-type]
        )
        root = cast("str", self.package)
        allowed = cast("set[str]", self.allowed_submodules)
        packages = {m[: -len(".descriptor")] for m in graph.modules if m.startswith(root + ".") and m.endswith(".descriptor")}
        packages |= cast("set[str]", self.extra_packages or set())
        ordered = sorted(packages, key=len, reverse=True)  # longest prefix wins for nested packages

        def owner(module: str) -> str | None:
            """Return the module package owning ``module`` (longest matching prefix), if any."""
            return next((p for p in ordered if module == p or module.startswith(p + ".")), None)

        violations: list[dict[str, object]] = []
        for importer in sorted(m for m in graph.modules if m.startswith(root + ".")):
            own = owner(importer)
            for imported in sorted(graph.find_modules_directly_imported_by(importer)):
                target = owner(imported)
                if target is None or target == own or imported == target:
                    continue
                if imported[len(target) + 1:].split(".")[0] in allowed:
                    continue
                lines = tuple(d["line_number"] for d in graph.get_import_details(importer=importer, imported=imported))
                violations.append({"importer": importer, "imported": imported, "lines": lines})
        if verbose:
            output.print(f"Checked {len(packages)} module packages; found {len(violations)} private imports.")
        return ContractCheck(
            kept=not violations,
            warnings=list(removal.warnings),
            ignored_import_count=removal.ignored_import_count,
            metadata={"violations": violations},
        )

    def render_broken_contract(self, check: ContractCheck) -> None:
        """Print each offending import as ``importer -> imported (lines)`` grouped by importer."""
        for item in check.metadata["violations"]:
            lines = ", ".join(f"l.{n}" for n in item["lines"])
            output.print_error(f"{item['importer']} -> {item['imported']} ({lines})", bold=False)
        output.new_line()
        output.print_error(
            "Import the owner's public module instead (add a DTO/query re-export or an approved read projection "
            "there), or document a justified exception in .importlinter ignore_imports.",
            bold=False,
        )

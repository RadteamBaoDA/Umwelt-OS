"""Provider docs must match the code catalog (P4-docs). Pure file and data checks; no I/O beyond docs."""

from pathlib import Path

from modules.connectors.catalog import list_catalog
from modules.connectors.provider_specs import DISPATCHABLE, FREE_PROVIDER_SPECS

DOCS = Path(__file__).resolve().parents[2] / "docs" / "connectors"
CATALOG_DOC = DOCS / "provider-catalog.md"
FREE_DOC = DOCS / "free-sources.md"


def _section(text: str, start: str, end: str) -> str:
    assert start in text, f"missing section {start!r}"
    return text.split(start, 1)[1].split(end, 1)[0]


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _rows_by_id(section: str) -> dict[str, list[list[str]]]:
    rows: dict[str, list[list[str]]] = {}
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        cells = _cells(line)
        rows.setdefault(cells[0].strip("`"), []).append(cells)
    return rows


def _inventory_rows() -> dict[str, list[list[str]]]:
    text = CATALOG_DOC.read_text(encoding="utf-8")
    return _rows_by_id(_section(text, "## Inventory", "## World and news sources"))


def test_every_catalog_id_listed_once_with_matching_facts() -> None:
    rows = _inventory_rows()
    mismatches: list[str] = []
    for entry in list_catalog():
        matches = rows.get(entry.provider_id, [])
        if len(matches) != 1:
            mismatches.append(f"{entry.provider_id}: {len(matches)} inventory rows")
            continue
        cells = matches[0]
        expected = [
            f"`{entry.availability}`",
            entry.eligibility,
            entry.execution or "-",
            "yes" if entry.code_available else "no",
            "yes" if entry.runtime_verified else "no",
            str(entry.default_interval_minutes or "-"),
            entry.terms_checked_on or "-",
        ]
        if cells[1:8] != expected:
            mismatches.append(f"{entry.provider_id}: {cells[1:8]} != {expected}")
    assert mismatches == []


def test_no_provider_claims_runtime_verification() -> None:
    rows = _inventory_rows()
    assert all(cells[5] == "no" for group in rows.values() for cells in group)
    assert all(not entry.runtime_verified for entry in list_catalog())
    assert all(not spec.runtime_verified for spec in FREE_PROVIDER_SPECS)


def test_free_presets_match_spec_eligibility_and_execution() -> None:
    text = FREE_DOC.read_text(encoding="utf-8")
    rows = _rows_by_id(_section(text, "## Catalog", "## Attribution"))
    for spec in FREE_PROVIDER_SPECS:
        matches = rows.get(spec.id, [])
        assert len(matches) == 1, spec.id
        cells = matches[0]
        assert cells[4] == spec.eligibility, spec.id
        assert cells[6] == spec.execution, spec.id


def test_registered_presets_are_the_dispatchable_set() -> None:
    text = FREE_DOC.read_text(encoding="utf-8")
    rows = _rows_by_id(_section(text, "## Catalog", "## Attribution"))
    registered = {pid for pid, group in rows.items() if group[0][7] == "registered"}
    assert registered == set(DISPATCHABLE)
    assert rows["coingecko"][0][7] == "not registered"

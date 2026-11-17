"""Pure validation of freshly read observation snapshot rows (private to the observations owner)."""

from uuid import UUID

from modules.knowledge.observations.models import Observation


def classify_snapshot_rows(
    rows: list[Observation],
    keys: tuple[tuple[str, UUID], ...],
    external_ids: tuple[str, ...],
    source_id: UUID,
    workspace_id: UUID,
) -> tuple[dict[tuple[str, UUID], Observation], dict[str, Observation]]:
    """Split the freshly read union rows into accepted pairs and per-key current rows.

    Pure in-memory validation: a row outside the workspace/source/requested namespace or with a
    non-positive revision, a duplicate accepted pair, or a second current row for one key raises
    ``RuntimeError`` with the same codes the caller previously raised inline.
    """
    accepted: dict[tuple[str, UUID], Observation] = {}
    current: dict[str, Observation] = {}
    for row in rows:
        if (
            row.workspace_id != workspace_id
            or row.source_id != source_id
            or row.external_id not in external_ids
            or row.revision <= 0
        ):
            raise RuntimeError("observation_preparation_namespace_changed")
        pair = (row.external_id, row.ingestion_observation_id)
        if pair in keys:
            if pair in accepted:
                raise RuntimeError("observation_preparation_duplicate_pair")
            accepted[pair] = row
        if row.is_current:
            if row.external_id in current:
                raise RuntimeError("observation_preparation_multiple_current")
            current[row.external_id] = row
    return accepted, current

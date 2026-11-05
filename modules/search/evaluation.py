def recall_at_k(ranked_ids: list[str], expected_ids: set[str], k: int) -> float:
    """Return the fraction of expected IDs present among the first k results."""
    top_k = set(ranked_ids[: max(0, k)])
    if not expected_ids:
        return 1.0 if not top_k else 0.0
    return len(top_k & expected_ids) / len(expected_ids)

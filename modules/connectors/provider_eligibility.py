"""Terms-eligibility decision for free providers, split out of ``provider_specs`` (pure, no I/O).

``provider_specs`` re-exports every public name here. This module only depends on the spec dataclass, so
it adds no import cycle.
"""

from dataclasses import dataclass
from typing import Literal

from modules.connectors.provider_specs_tables import FreeProviderSpec

DEPLOYMENT_USES: tuple[str, ...] = ("personal", "noncommercial", "commercial", "unknown")
_USE_RANK = {"personal": 0, "noncommercial": 1, "commercial": 2}


def deployment_use_matches(eligibility: str, deployment_use: str) -> bool:
    """Whether a catalog eligibility class admits the declared deployment use.

    open admits any declared use (attribution duties remain); personal admits only personal;
    noncommercial admits personal or noncommercial; review (and anything unrecognised) is always
    False here because it needs a separate persisted operator review.
    """
    if eligibility == "open":
        return True
    if eligibility == "personal":
        return deployment_use == "personal"
    if eligibility == "noncommercial":
        return deployment_use in {"personal", "noncommercial"}
    return False


@dataclass(frozen=True)
class OperatorReview:
    """Operator-recorded review evidence; never derived from an owner tick."""

    state: Literal["pending", "approved", "rejected"]
    allowed_use: str | None = None
    evidence_ref: str | None = None
    reviewed_terms_version: str | None = None


@dataclass(frozen=True)
class TermsDecision:
    """Eligibility outcome; ``code`` is a stable machine reason."""

    allowed: bool
    code: Literal["ok", "terms_not_acknowledged", "use_incompatible", "operator_review_required"]


def evaluate_terms(
    spec: FreeProviderSpec,
    *,
    declared_use: str | None,
    acknowledged_terms_version: str | None,
    review: OperatorReview | None = None,
) -> TermsDecision:
    """Deny by default. The single check shared by catalog readiness and source activation."""
    if not acknowledged_terms_version or declared_use not in DEPLOYMENT_USES:
        return TermsDecision(False, "terms_not_acknowledged")
    if spec.eligibility != "review":
        ok = deployment_use_matches(spec.eligibility, declared_use)
        return TermsDecision(ok, "ok" if ok else "use_incompatible")
    if (
        review is None
        or review.state != "approved"
        or not review.evidence_ref
        or review.reviewed_terms_version != acknowledged_terms_version
        or review.allowed_use not in _USE_RANK
    ):
        return TermsDecision(False, "operator_review_required")
    ok = declared_use in _USE_RANK and _USE_RANK[declared_use] <= _USE_RANK[review.allowed_use]
    return TermsDecision(ok, "ok" if ok else "use_incompatible")

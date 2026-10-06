"""CII v8 availability projection until authoritative activation evidence exists."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CiiProjection:
    """Keep the exact requested country selectors and unavailable score fields detached."""

    method_version: str
    requested_countries: tuple[str, ...]
    score: None
    band: None
    movement_24h: None
    as_of: None
    availability: str
    reason: str


def unavailable_v8_projection(requested_countries: list[str]) -> CiiProjection:
    """Return CII v8 as explicitly unavailable without substituting scores or country claims."""
    if len(requested_countries) > 31 or len(set(requested_countries)) != len(requested_countries):
        raise ValueError("CII country scope is invalid")
    if any(not code or len(code) > 8 or code != code.strip() for code in requested_countries):
        raise ValueError("CII country selector is invalid")
    return CiiProjection(
        method_version="v8", requested_countries=tuple(requested_countries),
        score=None, band=None, movement_24h=None, as_of=None,
        availability="method_data_license_unverified",
        reason="Authoritative v8 method, source licensing, and the specified country set are not verified.",
    )

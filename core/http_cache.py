"""Shared HTTP cache-control helpers for private owner routes."""

from fastapi import Response


def set_private_no_store(response: Response) -> None:
    """Mark a response as private and uncacheable (``Cache-Control: private, no-store``).

    Mutates only the supplied response headers; used by owner routes returning personal data.
    """
    response.headers["Cache-Control"] = "private, no-store"

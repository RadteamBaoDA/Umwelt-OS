"""Shared status-to-``DomainError`` factory for the ingestion native-collection internals."""

from core.errors import (
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
    PayloadTooLargeError,
    UnauthorizedError,
    ValidationFailedError,
)

_ERROR_BY_STATUS: dict[int, type[DomainError]] = {
    401: UnauthorizedError,
    403: ForbiddenError,
    404: NotFoundError,
    409: ConflictError,
    413: PayloadTooLargeError,
    422: ValidationFailedError,
}


def domain_error_for_status(status_code: int, detail: str) -> DomainError:
    """Build the client-visible ``DomainError`` subclass matching ``status_code`` (base class when unmapped).

    Pure; the caller raises the result. The status and detail are preserved exactly so HTTP rendering is unchanged.
    """
    return _ERROR_BY_STATUS.get(status_code, DomainError)(detail, status_code=status_code)

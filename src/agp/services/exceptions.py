"""Domain exceptions for the AGP service layer.

These decouple the service layer from any HTTP framework.  The API layer
catches them and maps to appropriate HTTP responses.
"""


class DomainError(Exception):
    """Base for all domain-layer errors."""
    def __init__(self, *, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class NotFoundError(DomainError):
    def __init__(self, detail: str):
        super().__init__(status_code=404, detail=detail)


class ConflictError(DomainError):
    def __init__(self, detail: str):
        super().__init__(status_code=409, detail=detail)


class BadRequestError(DomainError):
    def __init__(self, detail: str):
        super().__init__(status_code=400, detail=detail)


class InternalError(DomainError):
    def __init__(self, detail: str):
        super().__init__(status_code=500, detail=detail)

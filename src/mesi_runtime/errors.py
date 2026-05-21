from __future__ import annotations


class MesiError(Exception):
    """Base runtime error exposed as structured CLI/HTTP output."""

    status_code = 400
    code = "mesi_error"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code


class PathRejected(MesiError):
    status_code = 400
    code = "path_rejected"


class NotFound(MesiError):
    status_code = 404
    code = "not_found"


class Blocked(MesiError):
    status_code = 409
    code = "blocked"


class Conflict(MesiError):
    status_code = 409
    code = "conflict"


class Unsupported(MesiError):
    status_code = 422
    code = "unsupported"

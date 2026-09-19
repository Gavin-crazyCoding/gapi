"""gapi's own error type.

Serialized as ``{"error": {"code": ..., "message": ...}}`` with header
``X-Gapi-Error: 1`` so clients can distinguish gateway errors from upstream
error bodies, which are passed through verbatim.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


class GapiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def gapi_error_handler(_request: Request, exc: GapiError) -> JSONResponse:
    body: dict = {"error": {"code": exc.code, "message": exc.message}}
    if exc.details:
        body["error"]["details"] = exc.details
    return JSONResponse(
        status_code=exc.status_code,
        content=body,
        headers={"X-Gapi-Error": "1"},
    )


# Common constructors
def unauthorized(message: str = "Invalid or missing credentials") -> GapiError:
    return GapiError(401, "unauthorized", message)


def forbidden(message: str = "Account is inactive") -> GapiError:
    return GapiError(403, "forbidden", message)


def not_found(code: str = "not_found", message: str = "Not found") -> GapiError:
    return GapiError(404, code, message)


def payment_required(required: float, balance: float) -> GapiError:
    return GapiError(
        402,
        "insufficient_balance",
        "GavinCoin balance is insufficient for this request",
        {"required": required, "balance": balance},
    )

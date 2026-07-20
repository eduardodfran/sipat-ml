import logging
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

request_id_var: ContextVar[str] = ContextVar("request_id", default="")

logger = logging.getLogger(__name__)


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        req_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request_id_var.set(req_id)
        request.state.request_id = req_id

        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.error(
                "Unhandled exception on %s %s [%s] %.1fms: %s",
                request.method,
                request.url.path,
                req_id,
                elapsed_ms,
                exc,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "error": "internal_server_error",
                    "detail": "An unexpected error occurred",
                    "request_id": req_id,
                },
                headers={"X-Request-ID": req_id},
            )

        elapsed_ms = (time.monotonic() - start) * 1000
        response.headers["X-Request-ID"] = req_id

        # Skip logging health check probes to reduce noise
        if request.url.path.startswith("/health"):
            return response

        if response.status_code >= 400:
            logger.warning(
                "%s %s [%s] -> %d %.1fms",
                request.method,
                request.url.path,
                req_id,
                response.status_code,
                elapsed_ms,
            )
        else:
            logger.info(
                "%s %s [%s] -> %d %.1fms",
                request.method,
                request.url.path,
                req_id,
                response.status_code,
                elapsed_ms,
            )

        return response

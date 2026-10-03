"""Access boundary for the local, single-user trading dashboard."""
from ipaddress import ip_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


def local_access_allowed(request, origins):
    try:
        local = bool(request.client and ip_address(request.client.host).is_loopback)
    except ValueError:
        local = False
    origin = request.headers.get("origin")
    return (local and request.url.hostname in ("localhost", "127.0.0.1", "::1")
            and (not origin or origin in origins))


class LocalAccessMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, origins):
        super().__init__(app)
        self.origins = {origin.strip() for origin in origins if origin.strip()}

    async def dispatch(self, request, call_next):
        # Host validation also blocks DNS rebinding to a loopback address.
        if not local_access_allowed(request, self.origins):
            return JSONResponse({"detail": "Local dashboard access only"}, status_code=403)
        return await call_next(request)

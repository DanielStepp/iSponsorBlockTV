"""Password, CSRF check and security headers of the web UI."""

import base64
import hmac
from collections.abc import Awaitable, Callable

from aiohttp import web

# State-changing requests must carry this header (with a JSON body). A foreign website cannot
# send it without a CORS preflight, which this server never allows. Must match static/app.js.
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "iSponsorBlockTV"

# The page only loads its own script and stylesheet, nothing inline and nothing external.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
Middleware = Callable[[web.Request, Handler], Awaitable[web.StreamResponse]]


def _password_from_request(request: web.Request) -> str:
    """The password of the HTTP basic auth header (the user name is ignored)."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return ""
    try:
        return base64.b64decode(header[len("Basic ") :]).decode().partition(":")[2]
    except ValueError:  # invalid base64 or encoding
        return ""


def password_middleware(password: str) -> Middleware:
    """Require HTTP basic auth with ``password`` (any user name) for every request."""

    @web.middleware
    async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        given = _password_from_request(request)
        if not hmac.compare_digest(given.encode(), password.encode()):
            return web.Response(
                status=401,
                text="Authentication required",
                headers={"WWW-Authenticate": 'Basic realm="iSponsorBlockTV"'},
            )
        return await handler(request)

    return middleware


@web.middleware
async def csrf_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Reject POST requests that do not come from the page itself."""
    if request.method == "POST" and not (
        request.headers.get(CSRF_HEADER) == CSRF_VALUE
        and request.content_type == "application/json"
    ):
        return web.json_response({"error": "Invalid request"}, status=403)
    return await handler(request)


async def add_response_headers(request: web.Request, response: web.StreamResponse) -> None:
    """Add the security headers to every response. API answers must not be cached."""
    response.headers.update(SECURITY_HEADERS)
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"

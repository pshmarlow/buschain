"""The hosted entry point: `uvicorn --factory buschain.web.cloud:build_app`.

The local launcher (`__main__.py`) serves 127.0.0.1 to the one person at the
desk and needs no login. The hosted copy sits on the open internet, built from
a public repository, so anyone who learned its URL could read and rewrite the
live plan. This module is the lock on that door, and it is the only thing that
differs between the two ways of running the app. `create_app()` stays a pure
factory (the tests and the launcher build it directly) and is wrapped here
rather than taught to read the environment.
"""
from __future__ import annotations

import base64
import binascii
import os
import secrets

from starlette.types import ASGIApp, Receive, Scope, Send

from .app import create_app

# Long enough that guessing over the network is hopeless, which matters because
# there is deliberately no lockout: with one account, a lockout is a switch
# anyone on the internet can flip to lock the dispatcher out. A floor, not a
# policy. It exists to refuse a placeholder like "test" set by mistake, not to
# grade a real secret.
MIN_PASSWORD_LENGTH = 16

_BODY = b"Login required.\n"
_CHALLENGE = [
    # charset="UTF-8" tells the browser how to encode a non-ASCII password;
    # without it some browsers fall back to Latin-1 and the bytes never match.
    (b"www-authenticate", b'Basic realm="buschain", charset="UTF-8"'),
    (b"content-type", b"text/plain; charset=utf-8"),
    (b"content-length", str(len(_BODY)).encode("ascii")),
]


class _BasicAuth:
    """HTTP Basic auth in front of the whole app: API, page and scripts alike.

    Middleware rather than FastAPI's HTTPBasic dependency. A dependency guards
    only the routes it is attached to, and the StaticFiles mount is not a
    route, so index.html and app.js (and FastAPI's own /docs) would stay public.
    Pure ASGI rather than BaseHTTPMiddleware, whose response wrapping is known
    to break streaming and background tasks, at about the same length.

    Only the password is checked. With one account a username adds no secrecy,
    since all the entropy is in the password, and one secret is one thing to
    set and rotate. The browser still asks for a username; anything goes there.
    """

    def __init__(self, app: ASGIApp, password: str) -> None:
        self.app = app
        self.password = password.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # lifespan is the server talking to the app at startup and shutdown: no
        # request, no headers, nobody to ask. Every other scope must carry the
        # password, including kinds this app does not serve today, so a route
        # added later is guarded without anyone remembering to guard it.
        if scope["type"] == "lifespan" or self._authorized(scope):
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            # Closing before accept is how ASGI refuses a handshake (the
            # server answers 403); an HTTP response here would be a protocol
            # error.
            await send({"type": "websocket.close", "code": 1008})
        else:
            await send({"type": "http.response.start", "status": 401,
                        "headers": _CHALLENGE})
            await send({"type": "http.response.body", "body": _BODY})

    def _authorized(self, scope: Scope) -> bool:
        # A malformed header is a refusal, never an exception: a 500 would tell
        # a prober more than a 401 does, and would keep the browser from
        # offering the login prompt again.
        header = dict(scope.get("headers", [])).get(b"authorization", b"")
        scheme, _, encoded = header.partition(b" ")
        if scheme.lower() != b"basic":
            return False
        try:
            decoded = base64.b64decode(encoded.strip(), validate=True)
        except binascii.Error:
            return False
        _username, colon, password = decoded.partition(b":")
        # compare_digest so the time taken doesn't reveal how many leading
        # bytes of a guess were right.
        return bool(colon) and secrets.compare_digest(password, self.password)


def build_app() -> ASGIApp:
    """The app as the host runs it, configured from the environment.

    Refuses to start half-configured. Without BUSCHAIN_PASSWORD the app would
    be open to the internet. Without BUSCHAIN_DB, the quiet fallback to
    create_app's relative default would put the plan on the container's root
    filesystem, which is thrown away on every deploy: the hosted version of
    the "silently gives a new empty database" trap the README warns about. A
    crash at boot makes the deploy fail loudly instead.
    """
    db_path = os.environ.get("BUSCHAIN_DB", "")
    password = os.environ.get("BUSCHAIN_PASSWORD", "")
    if not db_path:
        raise RuntimeError("BUSCHAIN_DB is not set: refusing to start without "
                           "an explicit database path on the persistent volume")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise RuntimeError(f"BUSCHAIN_PASSWORD must be set to at least "
                           f"{MIN_PASSWORD_LENGTH} characters")
    return _BasicAuth(create_app(db_path), password)

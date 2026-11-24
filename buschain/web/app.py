"""FastAPI app factory. The web layer is thin by design: every route is
'mutate store -> re-run engine -> return state' or a plain read."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from ..store import db
from ..store.repo import Repo
from . import api

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_DB = Path("buschain.db")


class _RevalidatedStatic(StaticFiles):
    """The static page, served so the browser asks before reusing any of it.

    StaticFiles sends ETag and Last-Modified but no Cache-Control, which leaves
    the browser to guess: it may reuse a file without contacting the server for
    about a tenth of the time since that file last changed. Each file gets its
    own guess, and index.html and app.js are not independent — the script looks
    its controls up by id as it loads. Seen on 2026-11-24, right after a control
    moved from one toolbar to another: Chrome served index.html from cache with
    no request at all while fetching the new app.js, so the script asked for an
    element the page did not have. That throws at load, and every listener
    registered below the throw is never attached.

    `no-cache` means "keep a copy, but check it first", not "do not cache": an
    unchanged file still answers 304 with no body, so the cost is one round
    trip to localhost per file. `no-store` would buy nothing more and would
    resend all of app.js on every load.

    One thing this cannot reach: a copy the browser stored BEFORE this header
    existed carries no such instruction, and stays reusable until its guess
    runs out or the page is reloaded once.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        # A missing file raises inside super() and never gets here, so a 404
        # is left exactly as it was.
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(db_path: str | Path = DEFAULT_DB) -> FastAPI:
    app = FastAPI(title="buschain")
    app.state.repo = Repo(db.connect(db_path))
    app.include_router(api.router, prefix="/api")
    app.mount("/", _RevalidatedStatic(directory=STATIC_DIR, html=True), name="static")
    return app

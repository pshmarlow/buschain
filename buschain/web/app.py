"""FastAPI app factory. The web layer is thin by design: every route is
'mutate store -> re-run engine -> return state' or a plain read."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..store import db
from ..store.repo import Repo
from . import api

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_DB = Path("buschain.db")


def create_app(db_path: str | Path = DEFAULT_DB) -> FastAPI:
    app = FastAPI(title="buschain")
    app.state.repo = Repo(db.connect(db_path))
    app.include_router(api.router, prefix="/api")
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app

"""FastAPI app: REST API for the React UI + lifecycle for the LangGraph worker."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import settings
from app.api.routes import audit, core, ops, sim, sr_kb
from app.data import db, seed
from app.runtime import engine
from app.runtime.clock import clock

DIST = settings.REPO_DIR / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.configure()
    if not db.scalar("SELECT COUNT(*) FROM kb_articles", default=0):
        seed.run(reset=True)
    engine.graph()                       # compile once
    worker = engine.Worker()
    worker.start()
    app.state.worker = worker
    if not os.getenv("NEXA_SKIP_KPI"):
        from app.kpi import simulate
        simulate.ensure_results()
    yield
    worker.stop()


app = FastAPI(title="NexaCore Agentic Ops Intelligence", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["*"], allow_headers=["*"])
for r in (core.router, ops.router, sr_kb.router, sim.router, audit.router):
    app.include_router(r, prefix="/api")

if DIST.exists():                         # single-process demo: serve the built SPA from the API
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path == "api" or path.startswith("api/"):      # unknown API route: JSON 404, never the SPA's HTML
            raise HTTPException(404, f"no API route /{path}")
        f = DIST / path
        return FileResponse(f if path and f.is_file() else DIST / "index.html")

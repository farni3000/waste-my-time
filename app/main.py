from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR
from .db import SessionLocal, run_migrations
from .seed import seed_default_sources
from .web.routes import router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    run_migrations()
    with SessionLocal() as session:
        seed_default_sources(session)
    yield


app = FastAPI(title="waste-my-time", lifespan=lifespan)
app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "app" / "web" / "static")),
    name="static",
)
app.include_router(router)

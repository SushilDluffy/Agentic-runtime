"""Run with uvicorn examples.support.app:app --host 127.0.0.1."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agentic_runtime import Identity, OpenAIProvider, Runtime, SQLiteStore, Worker
from agentic_runtime.integrations.fastapi import create_router

from .domain import SupportDatabase, support_agent

DEMO_IDENTITY = Identity(user_id="demo-user", workspace_id="demo-workspace")


def create_app(directory: str | Path = ".demo", *, fault="none", provider=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    domain = SupportDatabase(directory / "support.sqlite", fault=fault)
    domain.seed(DEMO_IDENTITY)
    runtime = Runtime(
        SQLiteStore(directory / "runtime.sqlite"), [support_agent(domain, provider=provider)]
    )

    @asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()
        worker = asyncio.create_task(Worker(runtime).serve(stop))
        yield
        stop.set()
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    app = FastAPI(title="Agentic Runtime support demo", lifespan=lifespan)
    app.state.runtime = runtime
    app.state.domain = domain
    # Local demonstration identity. Production hosts must supply authenticated identities.
    app.include_router(create_router(runtime, lambda: DEMO_IDENTITY))
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(static / "index.html")

    return app


model = os.environ.get("OPENAI_MODEL")
app = create_app(
    os.environ.get("DEMO_DIRECTORY", ".demo"),
    fault=os.environ.get("DEMO_FAULT", "none"),
    provider=OpenAIProvider(model) if model else None,
)

"""The HTTP surface: health, piles, and document upload.

Runs and the gate live in `runs.py`. Both files are thin over
`app.operations`, which is what keeps this API, the MCP server and the review UI
able to do exactly the same things (graded behaviour 4).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import operations as ops
from app.api.runs import _translate, router as runs_router
from app.settings import REPO_ROOT, settings
from app.store.engine import fetch_one, transaction

log = logging.getLogger("doctask.api")


def _route_logs_to_uvicorn() -> None:
    """Make this application's logs actually appear.

    uvicorn configures handlers on its own loggers and leaves the root logger
    with none, so a `doctask.*` record at INFO propagates to a root that drops
    it. Everything the watcher says about what it dispatched, deferred and
    refused was therefore going nowhere -- which is the wrong failure for the
    one component whose whole job is to act without being asked.

    Borrowing uvicorn's handler rather than calling `basicConfig` keeps the
    watcher's lines in the same format and on the same stream as the request
    log, so `docker compose logs api` reads as one story.
    """
    doctask = logging.getLogger("doctask")
    if doctask.handlers:
        return
    uvicorn_error = logging.getLogger("uvicorn.error")
    for handler in uvicorn_error.handlers:
        doctask.addHandler(handler)
    if not doctask.handlers:
        logging.basicConfig(level=logging.INFO)
    doctask.setLevel(logging.INFO)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    """Start the watched location alongside the API, when it is switched on.

    In the same process on purpose. The watcher is a client of `operations` like
    everything else, so a second container would buy nothing but another thing
    to deploy -- and the per-pile advisory lock already makes it safe for the
    watcher and an HTTP caller to want the same pile at the same time.

    A misconfigured watcher fails loudly here and lets the API start anyway. The
    alternative -- refusing to serve because `WATCH_DIR` points somewhere
    unusable -- takes down the review gate over a feature the reviewer is not
    using. What is not acceptable is starting quietly and never dispatching, so
    the reason is logged and `GET /watch` reports it.
    """
    task: asyncio.Task | None = None
    stop = asyncio.Event()
    if settings.watch_enabled:
        from app import watch as watch_module

        _route_logs_to_uvicorn()

        try:
            watcher = watch_module.from_settings()
        except watch_module.WatchMisconfigured as exc:
            log.error("watch: not started -- %s", exc)
        else:
            task = asyncio.create_task(
                watch_module.run_forever(watcher, settings.watch_interval_seconds,
                                         stop))
    try:
        yield
    finally:
        if task is not None:
            stop.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="doctask", version="0.1.0", lifespan=lifespan)
app.include_router(runs_router)


@app.get("/health")
def health() -> dict:
    """Reports what is actually true, including when the database is down.

    A health endpoint that returns 200 while its database is unreachable is the
    kind of success message this system is not allowed to send.
    """
    try:
        with transaction() as conn:
            fetch_one(conn, "SELECT 1 AS ok")
        return {"status": "healthy", "database": "reachable"}
    except Exception as exc:
        raise HTTPException(503, {"status": "degraded", "database": str(exc)[:200]})


@app.get("/piles")
def list_piles() -> dict:
    return _translate(ops.list_piles)


@app.post("/piles")
def create_pile(name: str, domain: str = "vendor_contracts") -> dict:
    return _translate(lambda: ops.create_pile(name, domain))


@app.get("/piles/{pile_id}/documents")
def list_documents(pile_id: str) -> dict:
    return _translate(lambda: ops.list_documents(pile_id))


@app.post("/piles/{pile_id}/documents")
async def upload_document(pile_id: str, file: UploadFile) -> dict:
    """Send a document from your own machine. Stores it, reads it, halts at the
    gate.

    Large files come through here rather than through memory-resident JSON.

    This used to store the bytes and stop, which was the wrong contract: a
    caller got a success response for a document that never became a fact, never
    reached the register and never appeared at the review gate, and nothing said
    so. Uploading a document and having it read are the same intention, so this
    now runs the same `arrival` every other route into the system runs.
    """
    return _translate(lambda: ops.upload(pile_id, file.filename or "", _read(file)))


def _read(file: UploadFile) -> bytes:
    return file.file.read()


# ------------------------------------------------------------- the review UI --
#
# Served from the same origin as the API, under a prefix that cannot shadow an
# endpoint. The UI is a client of the routes above and of nothing else -- there
# is no server-rendered view and no UI-only endpoint, which is what keeps
# "anything a person can do here, a program can do too" a structural fact rather
# than a promise.
#
# Mounted only if the bundle is present. `docker build` produces it; a developer
# running uvicorn against a fresh clone has no `web/dist`, and refusing to start
# over a missing front-end would make the API depend on node.
_BUNDLE = REPO_ROOT / "web" / "dist"

if (_BUNDLE / "index.html").is_file():
    app.mount("/review", StaticFiles(directory=_BUNDLE, html=True), name="review")

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/review/")

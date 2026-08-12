"""The watched location.

The brief's third movement: "New documents keep arriving into a watched
location. Each arrival produces a focused update to the deliverable." The update
half is the graph and has been since the beginning. This file is the other half
-- the thing that notices.

## It is a trigger, not a pipeline

The single most important property here is that this module contains no
understanding of documents. It notices a file and calls `ops.arrival`, which is
the same operation `POST /arrivals` calls and the same one the
`doctask_document_arrived` MCP tool calls. Nothing is possible through the
watcher that is impossible through the other two, and nothing about how an
arrival is processed lives here.

That is not tidiness. The repository already learned this the expensive way: a
full run and an incremental update were two code paths, and a fix for entity
identity landed on one and not the other, silently. A watcher that grew its own
ingest path would be that bug again, with a filesystem event as its fuse.

## Polling, not inotify

A `watchdog` observer would be fewer lines and would be wrong here. The inbox is
a bind-mounted volume in `docker-compose.yml`, and inotify does not reliably
cross a bind mount between a host filesystem and a container -- a file the host
writes may raise no event inside the container at all. A watcher that works on
the developer's laptop and silently sees nothing in the deployment it ships in
is worse than no watcher.

Polling also gets the harder problem right for free. A file being copied into
the inbox appears immediately and finishes some time later, so an event-driven
watcher reads it half-written; this one requires a file's size and mtime to be
unchanged across two consecutive scans before touching it. The cost is up to one
interval of latency, and it is a good trade for never ingesting half a contract.

## What is not retried, and why

The retry question is answered by state that is already true rather than by a
claim this module takes out: bytes are either in the pile or they are not.

  * Already ingested into this pile -> skipped, no event, no run. This is the
    normal case on every tick after the first, and it costs one indexed lookup.
  * Refused or errored -> a `failed` row, and these bytes are never tried again.
    Without that a deterministically broken file is re-attempted every few
    seconds forever, and the log fills with the same failure until the inbox is
    cleaned by hand.
  * The pile is busy with another run -> nothing is recorded and the file is
    picked up on a later tick. Behaviour 9 says two runs at once stay two runs;
    the watcher's job when it loses that race is to wait, not to force it.

A process killed between the ingest and the event row re-dispatches next tick
into an ingest that is a content-addressed no-op. That is behaviour 2 holding
for the watcher without the watcher having to know anything about it.

Run it inside the API (`WATCH_ENABLED=true`, see `app/api/main.py`) or on its
own:

    python -m app.watch
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import operations as ops
from app.ingest.ingest import sha256_bytes
from app.settings import REPO_ROOT, settings
from app.store.engine import execute, fetch_all, fetch_one, transaction

log = logging.getLogger("doctask.watch")

CORPORA = REPO_ROOT / "corpora"

# Editors and sync clients litter a directory with partial files. None of these
# is a document, and ingesting one produces a gap row that says nothing useful.
IGNORED_SUFFIXES = (".part", ".crdownload", ".tmp", ".swp", "~")


class WatchMisconfigured(RuntimeError):
    """The watcher cannot run as configured, and says exactly why.

    Raised at startup rather than tolerated, because a watcher that starts and
    then quietly never dispatches anything is indistinguishable from a watcher
    that is working on an empty directory.
    """


@dataclass
class Outcome:
    """One decision about one file, in the vocabulary of `watch_event`."""

    filename: str
    outcome: str  # dispatched | duplicate | busy | failed | no_pile
    detail: str | None = None
    run_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "outcome": self.outcome,
            "detail": self.detail,
            "run_id": self.run_id,
        }


class Watcher:
    """Scans a directory and turns settled arrivals into `arrival` calls."""

    def __init__(
        self,
        directory: Path,
        pile_name: str,
        domain: str = "vendor_contracts",
        dispatch: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.directory = Path(directory)
        self.pile_name = pile_name
        self.domain = domain
        # Injected so a test can drive the whole loop -- scan, settle, dedupe,
        # record -- without a model, and so the production wiring is visibly a
        # call into `operations` rather than something bespoke.
        self.dispatch = dispatch or ops.arrival
        self._signatures: dict[str, tuple[int, int]] = {}
        self.scans = 0

        if not _is_under(self.directory, CORPORA):
            # `arrival` resolves its argument under corpora/ and refuses
            # anything outside it, which is a boundary worth keeping rather than
            # working around -- so the constraint is stated here, at startup,
            # instead of surfacing as a per-file rejection later.
            raise WatchMisconfigured(
                f"WATCH_DIR must be inside {CORPORA} so that arrivals resolve "
                f"through the same path check every other caller goes through; "
                f"got {self.directory}"
            )

    # ------------------------------------------------------------- scanning --

    def settled_files(self) -> list[Path]:
        """Files whose size and mtime have not moved since the previous scan.

        A file still being written changes on every scan and is therefore never
        returned, which is the whole point. A file that has finished arriving is
        returned on this scan *and every scan after it* -- settling is not a
        one-shot event -- so the duplicate check in `_handle` is what stops it
        being dispatched twice, not this method.
        """
        self.scans += 1
        if not self.directory.is_dir():
            self._signatures = {}
            return []

        current: dict[str, tuple[int, int]] = {}
        settled: list[Path] = []
        for path in sorted(self.directory.iterdir()):
            if not path.is_file() or _ignored(path):
                continue
            try:
                stat = path.stat()
            except OSError:
                # Vanished between listing and stat. Not an error: something is
                # still moving files around, and the next scan will see it.
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            current[path.name] = signature
            if self._signatures.get(path.name) == signature:
                settled.append(path)
        self._signatures = current
        return settled

    # ---------------------------------------------------------- dispatching --

    def tick(self) -> list[Outcome]:
        """One pass. Never raises: a watcher that dies on a bad file stops
        watching, and every later document is lost with no error anywhere."""
        outcomes: list[Outcome] = []
        for path in self.settled_files():
            try:
                outcome = self._handle(path)
            except Exception as exc:  # the loop must survive a bad file
                log.exception("watch: unhandled error on %s", path.name)
                outcome = Outcome(path.name, "failed", f"{type(exc).__name__}: {exc}")
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    def _handle(self, path: Path) -> Outcome | None:
        try:
            data = path.read_bytes()
        except OSError as exc:
            return Outcome(path.name, "failed", f"unreadable: {exc}")
        digest = sha256_bytes(data)

        pile_id = _pile_id(self.pile_name)
        if pile_id is None:
            # Nothing recorded, so this is retried once the pile exists. The
            # alternative -- a `failed` row -- would permanently refuse a
            # document because of a startup ordering problem.
            log.warning("watch: no pile named %r; %s left in place", self.pile_name, path.name)
            return Outcome(path.name, "no_pile", f"no pile named {self.pile_name!r}")

        if _already_handled(pile_id, digest):
            return None

        try:
            result = self.dispatch(pile_id, _relative(path), self.domain)
        except ops.PileBusy as exc:
            # Not recorded and not an error. Another run holds the pile; this
            # file is picked up on a later tick.
            log.info("watch: %s deferred, %s", path.name, exc)
            return Outcome(path.name, "busy", str(exc))
        except Exception as exc:  # recorded as a failure, never raised
            log.warning("watch: %s failed: %s", path.name, exc)
            _record(pile_id, path.name, digest, "failed", detail=f"{type(exc).__name__}: {exc}")
            return Outcome(path.name, "failed", f"{type(exc).__name__}: {exc}")

        run_id = result.get("run_id")
        note = result.get("note") or f"status {result.get('status')}"
        _record(pile_id, path.name, digest, "dispatched", detail=note, run_id=run_id)
        log.info("watch: %s -> run %s (%s)", path.name, run_id, note)
        return Outcome(path.name, "dispatched", note, run_id)


# ------------------------------------------------------------------ the loop --


async def run_forever(watcher: Watcher, interval: float, stop: asyncio.Event | None = None) -> None:
    """Tick until told to stop.

    `to_thread` because every operation below this line is blocking psycopg, and
    running it on the event loop would stall the API this watcher usually shares
    a process with.
    """
    stop = stop or asyncio.Event()
    log.info(
        "watch: watching %s for pile %r every %.1fs", watcher.directory, watcher.pile_name, interval
    )
    while not stop.is_set():
        try:
            await asyncio.to_thread(watcher.tick)
        except Exception:  # a failed tick must not end the loop
            log.exception("watch: tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue


def from_settings(dispatch: Callable[..., dict[str, Any]] | None = None) -> Watcher:
    return Watcher(
        settings.watch_dir, settings.watch_pile, settings.watch_domain, dispatch=dispatch
    )


# ---------------------------------------------------------------- reporting --


def status() -> dict[str, Any]:
    """What the watcher is configured to do and what it has recently done.

    Exposed on every surface because a watcher nobody can interrogate is a
    watcher nobody can tell is broken -- and "the inbox is empty" and "the
    watcher is not running" look identical from outside.
    """
    pile_id = _pile_id(settings.watch_pile)
    directory = settings.watch_dir
    body: dict[str, Any] = {
        "enabled": settings.watch_enabled,
        "directory": str(directory),
        "directory_exists": directory.is_dir(),
        "pile": settings.watch_pile,
        "pile_id": pile_id,
        "interval_seconds": settings.watch_interval_seconds,
        "pending_files": (
            sorted(p.name for p in directory.iterdir() if p.is_file() and not _ignored(p))
            if directory.is_dir()
            else []
        ),
    }
    if pile_id:
        with transaction() as conn:
            body["events"] = fetch_all(
                conn,
                """
                SELECT filename, outcome, detail, run_id, at
                FROM watch_event WHERE pile_id = %s
                ORDER BY at DESC LIMIT 50
            """,
                (pile_id,),
            )
    else:
        body["events"] = []
        body["note"] = (
            f"no pile named {settings.watch_pile!r}; arrivals are "
            f"deferred rather than failed until it exists"
        )
    return body


# ----------------------------------------------------------------- plumbing --


def _record(
    pile_id: str,
    filename: str,
    digest: str,
    outcome: str,
    detail: str | None = None,
    run_id: str | None = None,
) -> None:
    with transaction() as conn:
        execute(
            conn,
            """
            INSERT INTO watch_event (pile_id, filename, content_sha256, outcome,
                                     run_id, detail)
            VALUES (%s, %s, %s, %s, %s, %s)
        """,
            (pile_id, filename, digest, outcome, run_id, (detail or "")[:2000] or None),
        )


def _already_handled(pile_id: str, digest: str) -> bool:
    """True if these exact bytes have already had their answer.

    Two independent records, each covering the other's hole, because settling is
    not an event: a finished file is settled on every scan for as long as it sits
    in the inbox, so *something* has to remember what was done with it.

    `watch_event` is the primary record. A `dispatched` row means this watcher
    already turned these bytes into a run; a `failed` row means they cannot be
    processed and retrying on every tick would fill the log with the same
    failure until someone cleaned the inbox by hand.

    The document check is the backstop, and it covers the case the event table
    cannot: a process killed between the arrival's ingest and this module's
    insert leaves a document with no event. It also covers bytes that reached
    the pile through another surface entirely -- an HTTP upload of the same file
    is still the same file, and dispatching it again would produce a run whose
    only finding is that nothing changed.
    """
    with transaction() as conn:
        if fetch_one(
            conn,
            """
            SELECT 1 FROM watch_event
            WHERE pile_id = %s AND content_sha256 = %s
              AND outcome IN ('dispatched', 'failed')
        """,
            (pile_id, digest),
        ):
            return True
        return bool(
            fetch_one(
                conn,
                """
            SELECT 1 FROM document WHERE pile_id = %s AND content_sha256 = %s
        """,
                (pile_id, digest),
            )
        )


def _pile_id(name: str) -> str | None:
    with transaction() as conn:
        row = fetch_one(conn, "SELECT id FROM pile WHERE name = %s", (name,))
    return str(row["id"]) if row else None


def _relative(path: Path) -> str:
    return str(path.resolve().relative_to(CORPORA))


def _is_under(path: Path, parent: Path) -> bool:
    resolved = path.resolve()
    return resolved == parent.resolve() or parent.resolve() in resolved.parents


def _ignored(path: Path) -> bool:
    return path.name.startswith(".") or path.name.endswith(IGNORED_SUFFIXES)


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    watcher = from_settings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # not every platform has these
            loop.add_signal_handler(sig, stop.set)
    await run_forever(watcher, settings.watch_interval_seconds, stop)
    log.info("watch: stopped")


if __name__ == "__main__":
    asyncio.run(_main())


__all__ = [
    "Outcome",
    "WatchMisconfigured",
    "Watcher",
    "from_settings",
    "run_forever",
    "status",
]

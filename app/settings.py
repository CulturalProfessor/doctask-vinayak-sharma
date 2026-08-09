"""Process configuration. Values come from the environment; `.env` is read as a
convenience for local runs and never committed."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        # Real environment wins over the file, so container env is never
        # shadowed by a stray local .env.
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(REPO_ROOT / ".env")

# LangGraph pulls in langsmith, which ships a tracer that posts runs to a hosted
# service when switched on. It is off unless configured, but "off unless
# configured" is one stray environment variable away from a test suite that
# quietly reaches the network and a document pile that quietly leaves the
# machine. Behaviour 7 says the suite runs with no key and no network, so the
# default is pinned here rather than assumed.
for _tracing_flag in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
    os.environ.setdefault(_tracing_flag, "false")


def _flag(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = os.environ.get(
        "DATABASE_URL", "postgresql://postgres:postgres@localhost:5434/doctask"
    )
    # `fake` is the default on purpose: the test suite must run with no key and
    # no network. Switching to a live provider is a deliberate act.
    llm_provider: str = os.environ.get("LLM_PROVIDER", "fake")
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    model: str = os.environ.get("MODEL", "claude-sonnet-5")
    # OpenRouter, used to record fixtures without anyone needing a paid key.
    # The default model is priced at zero; recording is a one-time cost.
    openrouter_key: str = os.environ.get("OPEN_ROUTER_KEY", "")
    openrouter_model: str = os.environ.get(
        "OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free"
    )
    # The watched location (the brief's third movement). Off by default: a
    # background loop that starts itself in every test process and every import
    # of the app is a surprise, and one that dispatches runs is an expensive
    # one. `docker compose up` turns it on, because there the watcher is the
    # feature rather than a side effect.
    #
    # It must sit inside corpora/ -- `arrival` resolves paths under that root
    # and refuses anything outside it, and the watcher goes through the same
    # check as every other caller rather than around it.
    watch_enabled: bool = _flag("WATCH_ENABLED", False)
    watch_dir: Path = Path(os.environ.get("WATCH_DIR", str(REPO_ROOT / "corpora" / "inbox")))
    # Which pile arrivals land in. A name rather than an id, because the id of a
    # freshly seeded pile is not knowable when the container's environment is
    # written.
    watch_pile: str = os.environ.get("WATCH_PILE", "acme")
    watch_domain: str = os.environ.get("WATCH_DOMAIN", "vendor_contracts")
    # Two scans at this interval are needed before a file is considered settled,
    # so this is also half the worst-case latency from a file landing to a run
    # starting. Three seconds keeps a demo responsive without making the
    # database do useless work while the inbox is empty.
    watch_interval_seconds: float = float(os.environ.get("WATCH_INTERVAL_SECONDS", "3"))
    config_root: Path = REPO_ROOT / "config" / "domains"
    # Recorded model responses. Not test-only data -- these are what let the
    # demo and the container run with no key and no network, so they ship
    # with the application rather than living under tests/.
    fixture_root: Path = REPO_ROOT / "recordings" / "llm"
    # Cost guardrails. A run stops rather than quietly spending.
    max_model_calls_per_run: int = int(os.environ.get("MAX_MODEL_CALLS_PER_RUN", "200"))
    sample_mode: bool = _flag("SAMPLE_MODE", False)
    # When recording is on, a live provider writes its responses to the fixture
    # tree so the fake can replay them offline afterwards.
    record_fixtures: bool = _flag("RECORD_FIXTURES", False)


settings = Settings()

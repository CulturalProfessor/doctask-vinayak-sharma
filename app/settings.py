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
    watch_dir: Path = Path(os.environ.get("WATCH_DIR", str(REPO_ROOT / "corpora" / "inbox")))
    config_root: Path = REPO_ROOT / "config" / "domains"
    fixture_root: Path = REPO_ROOT / "tests" / "fixtures" / "llm"
    # Cost guardrails. A run stops rather than quietly spending.
    max_model_calls_per_run: int = int(os.environ.get("MAX_MODEL_CALLS_PER_RUN", "200"))
    sample_mode: bool = _flag("SAMPLE_MODE", False)
    # When recording is on, a live provider writes its responses to the fixture
    # tree so the fake can replay them offline afterwards.
    record_fixtures: bool = _flag("RECORD_FIXTURES", False)


settings = Settings()

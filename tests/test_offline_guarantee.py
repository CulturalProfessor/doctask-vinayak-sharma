"""Behaviour 7 says real tests run without a live key. That claim deserves a
test of its own rather than an assertion in a README."""
from __future__ import annotations


import pytest

from app.llm.base import ProviderError, get_provider
from app.llm.fake import FakeProvider


def test_the_default_provider_needs_no_key():
    """If this ever flips, the suite starts silently costing money."""
    assert isinstance(get_provider(), FakeProvider)


def test_live_provider_without_a_key_refuses_clearly(monkeypatch):
    """And the refusal has to name the way out, not just fail."""
    import dataclasses

    import app.llm.anthropic as anthropic_mod
    from app.settings import settings

    monkeypatch.setattr(
        anthropic_mod, "settings", dataclasses.replace(settings, anthropic_api_key="")
    )
    with pytest.raises(ProviderError, match="LLM_PROVIDER=fake"):
        anthropic_mod.AnthropicProvider()


def test_unknown_provider_is_rejected():
    with pytest.raises(ProviderError, match="unknown LLM_PROVIDER"):
        get_provider("gpt-please")


def test_the_app_imports_with_no_secrets_in_the_environment(monkeypatch):
    """CI has no secrets. Import-time dependence on one would fail there and
    nowhere else, which is the worst place to discover it."""
    for var in ("ANTHROPIC_API_KEY", "SUPERDOCS_API_KEY", "DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    import importlib

    import app.api.main
    import app.seed
    import app.settings

    for module in (app.settings, app.api.main, app.seed):
        importlib.reload(module)


def test_the_offline_default_cannot_drift_silently():
    """If this default ever flips to a live provider, every test run starts
    costing money and reaching the network. It is worth pinning."""
    from app.settings import Settings

    assert Settings.__dataclass_fields__["llm_provider"].default == "fake"

"""Configuration over code is a graded property, so the config loader gets the
same scrutiny as the code: it must load the real domain, and it must refuse a
broken one loudly rather than failing three stages into a run."""
from __future__ import annotations

import pytest
import yaml

from app.domain.config import ConfigError, load_domain


def test_vendor_contracts_domain_loads():
    cfg = load_domain("vendor_contracts")
    assert set(cfg.doc_types) == {"msa", "amendment", "sow", "invoice", "notice"}
    assert cfg.rules["hours_within_sow_cap"].severity == "high"
    assert "hourly_rate" in cfg.extraction["msa"]["fields"]


def test_precedence_ranks_by_contractual_authority():
    """An amendment supersedes the agreement it amends, which supersedes a SOW
    issued under it. This ranking only ever *proposes* a resolution -- the
    conflict still goes to the gate."""
    cfg = load_domain("vendor_contracts")
    assert cfg.precedence_of("amendment") > cfg.precedence_of("msa")
    assert cfg.precedence_of("msa") > cfg.precedence_of("sow")
    assert cfg.precedence_of(None) == 0


def test_an_invoice_never_outranks_the_agreement_it_bills_against():
    """The bug this guards against: rank invoices highest and the system
    proposes the billing error as the correct rate. An invoice is evidence of
    what was billed, not authority on what should have been."""
    cfg = load_domain("vendor_contracts")
    assert cfg.precedence_of("invoice") < cfg.precedence_of("msa")
    assert cfg.precedence_of("invoice") < cfg.precedence_of("amendment")
    assert cfg.precedence_of("invoice") == min(
        cfg.precedence_of(t) for t in cfg.doc_types
    )


def test_every_doc_type_has_a_loadable_extraction_schema():
    cfg = load_domain("vendor_contracts")
    for spec in cfg.doc_types.values():
        assert spec.extraction, f"{spec.key} declares no extraction schema"
        schema = cfg.extraction[spec.extraction]
        assert schema["fields"], f"{spec.extraction} declares no fields"
        assert "{counterparty_slug}" in schema["entity_key"]


def _write_domain(tmp_path, **overrides):
    """A minimal valid domain on disk, with targeted breakage applied."""
    root = tmp_path / "toy"
    (root / "extraction").mkdir(parents=True)
    (root / "rules").mkdir(parents=True)
    files = {
        "doc_types.yaml": {"doc_types": {"memo": {"label": "Memo", "extraction": "memo"}}},
        "extraction/memo.yaml": {"entity_key": "e:{counterparty_slug}",
                                 "fields": {"who": {"type": "text"}}},
        "normalization.yaml": {"accepted_formats": ["txt"]},
        "reconciliation.yaml": {"precedence": ["memo"]},
        "rules/playbook.yaml": {"rules": {"r1": {"statement": "s", "applies_to": ["memo"]}}},
        "register.yaml": {"columns": [{"field": "term"}]},
    }
    files.update(overrides)
    for name, body in files.items():
        (root / name).write_text(yaml.safe_dump(body))
    return root


def test_broken_config_fails_at_load_not_mid_run(tmp_path):
    load_domain.cache_clear()
    root = _write_domain(
        tmp_path,
        **{"rules/playbook.yaml": {"rules": {"r1": {"statement": "s",
                                                   "applies_to": ["nonexistent"]}}}},
    )
    with pytest.raises(ConfigError, match="unknown doc types"):
        load_domain("toy", root=root.parent)


def test_extraction_schema_pointing_nowhere_is_caught(tmp_path):
    load_domain.cache_clear()
    root = _write_domain(
        tmp_path,
        **{"doc_types.yaml": {"doc_types": {"memo": {"label": "Memo", "extraction": "absent"}}}},
    )
    with pytest.raises(ConfigError):
        load_domain("toy", root=root.parent)


def test_unknown_domain_is_an_error(tmp_path):
    load_domain.cache_clear()
    with pytest.raises(ConfigError, match="unknown domain"):
        load_domain("no_such_domain", root=tmp_path)

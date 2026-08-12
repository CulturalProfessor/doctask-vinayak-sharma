"""Domain configuration loader.

Configuration over code: a new document type, extraction field, normalisation
rule or checklist item is a YAML change under `config/domains/<domain>/`. If
adding one of those needs Python, the design is wrong -- see TASK.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.settings import settings


class ConfigError(RuntimeError):
    """Raised when a domain's configuration is missing or self-inconsistent.

    Loud and early: a misconfigured domain should fail at load, not halfway
    through a run that has already spent money.
    """


@dataclass(frozen=True)
class DocTypeSpec:
    key: str
    label: str
    hints: list[str] = field(default_factory=list)
    extraction: str | None = None  # name of the extraction schema file


@dataclass(frozen=True)
class RuleSpec:
    key: str
    statement: str
    severity: str
    applies_to: list[str] = field(default_factory=list)
    # How the rule is actually evaluated: a check kind from
    # app/stages/examine.py plus the fields it names. A rule without one is
    # reported as unjudgeable rather than quietly skipped.
    check: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DomainConfig:
    name: str
    root: Path
    doc_types: dict[str, DocTypeSpec]
    extraction: dict[str, dict[str, Any]]
    normalization: dict[str, Any]
    reconciliation: dict[str, Any]
    rules: dict[str, RuleSpec]
    register: dict[str, Any]

    @property
    def accepted_formats(self) -> list[str]:
        return list(
            self.normalization.get("accepted_formats", ["txt", "md", "html", "pdf", "docx"])
        )

    def precedence_of(self, doc_type: str | None) -> int:
        """Higher wins. Used to *propose* a conflict resolution, never to apply
        one -- the conflict still goes to the human gate."""
        order = self.reconciliation.get("precedence", [])
        if doc_type in order:
            return order.index(doc_type) + 1
        return 0


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return data


@lru_cache(maxsize=8)
def load_domain(name: str, root: Path | None = None) -> DomainConfig:
    base = (root or settings.config_root) / name
    if not base.is_dir():
        raise ConfigError(f"unknown domain {name!r}: no directory at {base}")

    raw_types = _read_yaml(base / "doc_types.yaml").get("doc_types", {})
    doc_types = {
        key: DocTypeSpec(
            key=key,
            label=spec.get("label", key),
            hints=list(spec.get("hints", [])),
            extraction=spec.get("extraction"),
        )
        for key, spec in raw_types.items()
    }
    if not doc_types:
        raise ConfigError(f"{base/'doc_types.yaml'} declares no doc_types")

    extraction: dict[str, dict[str, Any]] = {}
    for spec in doc_types.values():
        if spec.extraction:
            extraction[spec.extraction] = _read_yaml(
                base / "extraction" / f"{spec.extraction}.yaml"
            )

    raw_rules = _read_yaml(base / "rules" / "playbook.yaml").get("rules", {})
    rules = {
        key: RuleSpec(
            key=key,
            statement=r["statement"],
            severity=r.get("severity", "medium"),
            applies_to=list(r.get("applies_to", [])),
            check=dict(r.get("check") or {}),
        )
        for key, r in raw_rules.items()
    }

    cfg = DomainConfig(
        name=name,
        root=base,
        doc_types=doc_types,
        extraction=extraction,
        normalization=_read_yaml(base / "normalization.yaml"),
        reconciliation=_read_yaml(base / "reconciliation.yaml"),
        rules=rules,
        register=_read_yaml(base / "register.yaml"),
    )
    _validate(cfg)
    return cfg


def _validate(cfg: DomainConfig) -> None:
    """Catch the config mistakes that would otherwise surface as a confusing
    failure three stages into a run."""
    for spec in cfg.doc_types.values():
        if spec.extraction and spec.extraction not in cfg.extraction:
            raise ConfigError(
                f"doc_type {spec.key!r} points at extraction schema {spec.extraction!r} "
                f"but {cfg.root/'extraction'}/{spec.extraction}.yaml was not loaded"
            )
    for rule in cfg.rules.values():
        unknown = [t for t in rule.applies_to if t not in cfg.doc_types]
        if unknown:
            raise ConfigError(f"rule {rule.key!r} applies_to unknown doc types: {unknown}")
        # A check kind that does not exist would make a rule silently stop being
        # enforced, and a playbook whose rules quietly do nothing is worse than
        # no playbook. Caught at load, not three stages into a run.
        from app.stages.examine import KNOWN_CHECKS

        kind = rule.check.get("kind")
        if kind is not None and kind not in KNOWN_CHECKS:
            raise ConfigError(
                f"rule {rule.key!r} uses unknown check kind {kind!r}; known "
                f"kinds are {', '.join(sorted(KNOWN_CHECKS))}"
            )
        missing = [p for p in KNOWN_CHECKS.get(kind, ()) if p not in rule.check]
        if missing:
            raise ConfigError(
                f"rule {rule.key!r} check {kind!r} needs {missing} but the "
                f"playbook does not give them"
            )
    for column in cfg.register.get("columns", []):
        if "field" not in column:
            raise ConfigError(f"register column {column!r} has no 'field'")
    known_types = set(cfg.doc_types) | {None}
    for t in cfg.reconciliation.get("precedence", []):
        if t not in known_types:
            raise ConfigError(f"reconciliation precedence names unknown doc type {t!r}")

    # A typo in instance_fields silently re-enables noisy conflicts for that
    # field, which is the kind of failure nobody notices until a demo.
    known_fields = {name for schema in cfg.extraction.values() for name in schema.get("fields", {})}
    for key in ("instance_fields", "time_varying_fields", "identity_fields"):
        unknown = [f for f in cfg.reconciliation.get(key, []) if f not in known_fields]
        if unknown:
            raise ConfigError(
                f"reconciliation {key} names fields that no extraction schema "
                f"declares: {unknown}"
            )

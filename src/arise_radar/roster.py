"""Loading and validating YAML researcher rosters."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from arise_radar.models import Researcher, SeedResearcher


class RosterError(RuntimeError):
    """Raised when a roster file is missing, malformed, or fails schema validation."""


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise RosterError(f"Roster file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise RosterError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RosterError(f"Roster file {path} must contain a mapping with a 'researchers' key")
    return data


def load_roster(path: Path) -> list[Researcher]:
    """Load the production roster: verified records ready to query."""
    data = _load_yaml(path)
    entries = data.get("researchers", [])
    try:
        return [Researcher.model_validate(entry) for entry in entries]
    except ValidationError as exc:
        raise RosterError(f"Invalid researcher record in {path}: {exc}") from exc


def load_seed_roster(path: Path) -> list[SeedResearcher]:
    """Load the names-only seed roster used while verifying OpenAlex IDs."""
    data = _load_yaml(path)
    entries = data.get("researchers", [])
    try:
        return [SeedResearcher.model_validate(entry) for entry in entries]
    except ValidationError as exc:
        raise RosterError(f"Invalid seed record in {path}: {exc}") from exc


def active_verified_researchers(researchers: list[Researcher]) -> list[Researcher]:
    """Publication/citation-active researchers: a confirmed scholarly
    identity the pipeline actually queries against OpenAlex. Ambiguous
    identities (e.g. a merged OpenAlex author node, resolved via the
    relevance filter) still count as active/verified here — only
    `identity_status == "unverified"` or `active: false` excludes a record.
    """
    return [r for r in researchers if r.active and r.identity_status != "unverified"]


def unverified_researchers(researchers: list[Researcher]) -> list[Researcher]:
    """Media-monitoring-only names: carried on the roster for network
    completeness, but with no confirmed scholarly identity yet, so never
    queried against OpenAlex until reviewed and promoted."""
    return [r for r in researchers if r.identity_status == "unverified"]

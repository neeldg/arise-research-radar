"""Same-run deduplication by canonical key (the "Dedupe" pipeline stage).

`upsert_publication` (sinks/notion.py) already merges researcher names into
an *existing* Notion row when it finds one — but that only works once a row
has actually been written. In `--notion-dry-run` mode nothing is ever
written, so without this step, two roster researchers who both authored the
same shared paper would each independently report "would create" for the
same canonical key, looking like duplicate live writes instead of one merged
row (see scripts/run_roster.py). Grouping by canonical key here, before any
Notion call, fixes that for both dry-run and live runs and gives the
"Classify"/version-family stages a clean, one-record-per-canonical-key batch
to work with.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from arise_radar.models import NormalizedPublication
from arise_radar.relevance import RelevanceDecision


class DedupedPublication(BaseModel):
    """One canonical key's worth of same-run matches, merged.

    `publication` is the first-seen record for this canonical key (used as
    the representative for title/metadata); `researcher_names` is every
    roster researcher who matched it in this run, sorted and de-duplicated.
    """

    publication: NormalizedPublication
    researcher_names: list[str]
    decision: RelevanceDecision


def group_by_canonical_key(
    entries: Sequence[tuple[NormalizedPublication, RelevanceDecision]],
) -> list[DedupedPublication]:
    """Merge same-run entries that share a canonical key into one
    DedupedPublication each, preserving first-seen order. Never touches a
    canonical key's value — only groups records that already have the same
    one (see arise_radar.version_family for cross-DOI probable-duplicate
    flagging, which is deliberately separate and never merges).
    """
    groups: dict[str, DedupedPublication] = {}
    order: list[str] = []

    for publication, decision in entries:
        key = publication.canonical_key
        if key not in groups:
            groups[key] = DedupedPublication(
                publication=publication,
                researcher_names=[publication.researcher_name],
                decision=decision,
            )
            order.append(key)
        elif publication.researcher_name not in groups[key].researcher_names:
            groups[key].researcher_names.append(publication.researcher_name)

    for group in groups.values():
        group.researcher_names.sort()

    return [groups[key] for key in order]

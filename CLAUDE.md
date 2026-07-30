# ARISE Research Radar — Claude Code Instructions

## Project overview

ARISE Research Radar is a scheduled research and media-monitoring system for ARISE AI.

ARISE currently maintains a Notion queue containing research papers, citations, media coverage, and other items that may be shared through its social media channels.

Ethan Goh currently performs much of this work manually by reading broadly, finding relevant items, adding them to Notion, and preparing summaries or social posts.

The goal of this project is to automate the discovery and preparation portions of that workflow while preserving human editorial review.

## Core product goal

The system should detect when:

1. An ARISE-affiliated researcher publishes a new paper.
2. A new scholarly paper cites or builds on an ARISE study.
3. A news article, blog post, institutional announcement, newsletter, podcast page, or other public source mentions:

   * ARISE,
   * an ARISE-affiliated researcher,
   * research involving an ARISE affiliate,
   * or findings from an ARISE-related study.

After detecting an item, the system should eventually:

1. Normalize its metadata.
2. determine how it relates to ARISE.
3. Check whether it already exists in the Notion queue.
4. Create or update a Notion entry.
5. Generate an internal summary.
6. Generate an editable social-media draft.
7. Leave the item for human review.

The system must never publish social content automatically.

## Important distinction

The product may be described as an “agent,” but the implementation should primarily be a deterministic scheduled pipeline:

```text
Fetch
  ↓
Normalize
  ↓
Match
  ↓
Dedupe
  ↓
Classify
  ↓
Summarize
  ↓
Write to Notion
```

Use model calls only where judgment or language generation is required.

Do not introduce an autonomous agent loop unless there is a clear technical reason.

## Monitoring pipelines

### 1. Roster publication pipeline

Purpose:

> Detect newly published work by ARISE-affiliated researchers.

Rules:

* Maintain a configured roster of ARISE researchers.
* Query academic APIs using stable identifiers, especially OpenAlex author IDs and ORCID IDs.
* Never use researcher names as the primary academic publication lookup.
* Name search can be used only as supplementary evidence.
* Every verified publication by a roster member should be retained for human review.
* Roster results should not be removed by a general relevance classifier.

### 2. Scholarly citation pipeline

Purpose:

> Detect newly published papers that cite or build on registered ARISE studies.

Rules:

* Maintain a registry of ARISE papers.
* Prefer DOI, OpenAlex work ID, PubMed ID, arXiv ID, and medRxiv ID.
* Store both the citing work and the ARISE work being cited.
* Later classify whether the citation:

  * applies the study,
  * extends it,
  * validates it,
  * critiques it,
  * or only mentions it as background.
* Citation classification is advisory and must remain reviewable.

### 3. Media and public-mention pipeline

Purpose:

> Detect public coverage connected to ARISE, its affiliates, or their research.

This pipeline must not rely exclusively on DOI-linked coverage.

Ordinary articles often:

* mention an ARISE affiliate without naming ARISE,
* discuss an affiliate’s research without citing a paper,
* paraphrase study findings without including the DOI,
* or interview an affiliate about a broader topic.

Detection should combine:

* ARISE and organization-name variants,
* affiliate names and name variants,
* paper titles and title variants,
* DOI and publication identifiers,
* institutional affiliations,
* distinctive study terms,
* research topics,
* alert feeds,
* RSS feeds,
* news search,
* and structured media signals where available.

The system should preserve the evidence that caused an item to match.

### 4. General field discovery

Purpose:

> Detect important developments in clinical AI that are not directly connected to ARISE.

This is secondary.

Do not build the broad field-discovery pipeline until the roster, citation, and media pipelines work reliably and there is evidence it adds value beyond Google Scholar, Google Alerts, newsletters, and journal alerts.

## Technical stack

Use:

* Python 3.12
* Pydantic
* httpx
* PyYAML
* pytest
* OpenAlex
* PubMed E-utilities
* Crossref metadata where useful
* ORCID where useful
* Anthropic API for structured classification and drafting
* Notion API as the operational queue
* GitHub Actions for scheduling

Potential media inputs may include:

* Google Alerts delivered by email or RSS
* curated RSS feeds
* institutional press pages
* news search APIs
* structured media-monitoring services

Prefer documented APIs, feeds, and alert mechanisms over brittle scraping.

Do not bypass paywalls or access restrictions.

## Architectural rules

### Normalize first

All sources must normalize into common internal record types before downstream processing.

Source adapters should not contain Notion-specific or LLM-specific logic.

Conceptually:

```text
External source response
        ↓
Source adapter
        ↓
Normalized record
        ↓
Shared downstream pipeline
```

### Stable identifiers

Use stable identifiers whenever available.

For researchers:

* OpenAlex author ID
* ORCID

For publications:

* DOI
* OpenAlex work ID
* PubMed ID
* PMC ID
* arXiv ID
* medRxiv ID

Names and titles are fallback matching signals, not primary identity keys.

### Idempotent writes

Repeated runs must not create duplicate Notion entries.

Use deterministic canonical keys and query before creating.

### Failure behavior

A source failure, classifier failure, or summary failure must be visible.

Do not silently discard candidates.

If an LLM call fails:

* retain the item,
* leave generated fields empty,
* store the error,
* and mark it as needing attention.

### Provenance

Every item must preserve:

* the original source,
* the original URL,
* source-specific identifiers,
* matched terms,
* related affiliates,
* related ARISE papers,
* and the reason it was considered relevant.

### Human review

Generated summaries and social drafts are always drafts.

The system must not:

* autonomously approve content,
* schedule posts,
* publish posts,
* or imply ARISE participation where there was only a citation or indirect mention.

## Deduplication strategy

Eventually use multiple passes:

1. Exact DOI or stable identifier
2. Exact normalized title
3. Fuzzy title similarity
4. Canonical URL matching
5. Preprint-to-journal-version matching
6. Grouping multiple media articles around the same underlying study

Do not implement broad deduplication during the first vertical slice.

## Notion’s role

Notion will eventually be the operational source of truth.

Expected fields include:

* Title
* Item type
* Source
* Canonical key
* URL
* DOI
* Publication date
* Detection date
* Relevant affiliate
* Related ARISE paper
* Relationship type
* Match evidence
* Confidence
* Internal summary
* Draft social post
* Review status
* Editorial notes
* Error state

Rejected items should remain stored because they are important evaluation data and prevent repeated resurfacing.

## Evaluation strategy

The system must be tested in shadow mode against Ethan’s existing manual workflow.

Track:

* items Ethan found that the system missed,
* items the system found that Ethan missed,
* false positives,
* duplicate rate,
* approval rate,
* time from release to detection,
* relationship-classification accuracy,
* and editing required for generated drafts.

Store rejected items and rejection reasons prospectively.

Do not evaluate only on items that ARISE ultimately posts.

## Rollout order

1. Local roster-publication dry run
2. Roster pipeline writing to a test Notion database
3. Internal summaries and draft generation
4. ARISE paper registry
5. Scholarly citation monitoring
6. Media and public-mention monitoring
7. Shadow-mode evaluation
8. Broader field discovery, only if justified

## Completed milestones

* **Roster-publication dry run.** YAML researcher roster, normalized
  publication record, an injectable OpenAlex source adapter with cursor
  pagination and bounded retries, a local CLI that fetches recent works by
  OpenAlex author ID, readable terminal output, mocked tests.
* **Publication-level relevance filter.** For roster entries with an
  ambiguous/merged OpenAlex identity (`identity_status: ambiguous`,
  `relevance_filter: healthcare_arise`), a deterministic, fail-open
  keyword/topic filter over OpenAlex metadata (title, topics, concepts,
  venue, work type) that keeps healthcare/clinical-AI-relevant papers,
  excludes clearly unrelated-domain papers (with a visible, auditable
  exclusion summary — never silently discarded), and marks ambiguous ones
  `uncertain` but still keeps them. No LLM calls.

## Completed milestones (continued)

* **Local test Notion sink for the roster-publication pipeline.** Notion
  configuration (`NOTION_TOKEN`, `NOTION_DATA_SOURCE_ID`) loaded from
  environment variables (`.env` via `python-dotenv`, working-directory-aware
  via `find_dotenv(usecwd=True)`), never printed or logged; a typed Notion
  sink (`src/arise_radar/sinks/notion.py`) using a small injectable HTTP
  client mirroring the OpenAlex adapter's pattern (tests mock it via
  `httpx.MockTransport` rather than the official `notion-client` SDK, whose
  support for the newer "data source" API surface wasn't relied on);
  canonical-key lookup and create-or-update (upsert): no match creates a
  page with `Status = New`, exactly one match updates metadata while
  preserving the existing editorial `Status` and merging the current
  researcher into `Researchers`, more than one match is left untouched and
  logged as a visible duplicate-key error; excluded publications are never
  written; a Notion failure on one publication doesn't stop the run;
  explicit CLI opt-in flags (`--write-notion`, `--notion-dry-run`, mutually
  exclusive, default fully read-only); `scripts/setup_notion.py` to
  provision the test database + data source from a verified parent page
  (confirmed live: database creation and data-source-schema creation must
  be two separate calls — `POST /v1/databases` silently ignores an inline
  `properties` field).

## Completed milestones (continued, 2)

* **Notion schema migration.** A safe, idempotent, additive-only migration
  (`scripts/update_notion_schema.py`) against the existing
  `NOTION_DATA_SOURCE_ID`: adds `Internal Summary`, `Key Story Angle`, `Why
  It Matters`, `Draft Social Post`, `Draft Status`, `Draft Error`, `Drafted
  Date`, `Draft Source Basis`, `Draft Model` if they don't already exist;
  never touches an existing property (a conflicting type is skipped and
  reported, never overwritten); `--dry-run` makes no write requests.
* **Canonical ARISE LinkedIn style reference.** `config/prompts/
  arise_linkedin_example.md` (the verbatim user-supplied exemplar) and
  `config/prompts/arise_linkedin_style.md` (distilled style principles,
  explicitly marked as a style reference only — never a source of facts).

## Completed milestones (continued, 3)

* **Pre-live-import roster safeguards.** Two narrow, deterministic
  "Classify"/"Dedupe" pipeline stages (not the deferred broad fuzzy
  deduplication — see "Deduplication strategy" above, which is still not
  built), added ahead of the first 15-author live import after reviewing a
  30-day roster dry run:
  * **Work-type classification** (`src/arise_radar/work_types.py`):
    classifies every discovered work as `article`, `preprint`, `conference`,
    `editorial/viewpoint`, `protocol/registration`, `dataset/repository`, or
    `unknown`, using OpenAlex type + venue + DOI prefix + title together
    (never discards Zenodo/OSF records outright — both host a genuine mix of
    conventional papers and other research objects). All records are still
    retained (fail-open); the three non-eligible categories default to
    `Draft Status = Needs Attention` with a clear `Draft Error` reason at
    creation time only (never overwritten on resync, preserving human/
    drafting-pipeline progress).
  * **Version-family (possible-duplicate) flagging**
    (`src/arise_radar/version_family.py`): strongly-normalized title
    similarity (`difflib`, threshold 0.90) plus overlapping full paper
    authorships (now captured via `NormalizedPublication.authors`, extracted
    from OpenAlex `authorships` at no extra API cost) — both signals
    required. Purely advisory: DOI canonical keys are never merged or
    altered; a clear note is appended to `System Notes` linking the probable
    other version(s) for a human to decide.
  * **Same-run dedup before any Notion call**
    (`src/arise_radar/dedupe.py`, `group_by_canonical_key`): a paper shared
    by multiple roster researchers is grouped into one row *before*
    `upsert_publication` is ever called, so `--notion-dry-run` (which never
    persists between calls) reports one merged row instead of one
    misleading "Would create" line per matching author.
    `upsert_publication` gained an optional `researcher_names` param for
    this (defaults to `{publication.researcher_name}`, fully backward
    compatible).
  * **Aggregate run summary** (`format_run_summary` in
    `src/arise_radar/output.py`, printed once at the end of
    `scripts/run_roster.py`): raw author-work matches, unique canonical
    keys, existing/proposed-new rows (when Notion is enabled), shared-author
    works, possible version duplicates, and non-standard research objects.
  * No new Notion schema property was introduced (avoids needing a schema
    migration run before go-live); everything surfaces through the existing
    `System Notes`, `Draft Status`, and `Draft Error` fields.

## Completed milestones (continued, 4)

* **Citation monitoring, Phase 1** (`scripts/run_citations.py`,
  `src/arise_radar/citations.py`, `src/arise_radar/sinks/notion_citations.py`).
  Detects citation relationships to tracked ARISE papers via OpenAlex and
  syncs them idempotently into a **separate** Citation Events Notion data
  source (`NOTION_CITATIONS_DATA_SOURCE_ID`) — the publications data source
  and schema are never touched. Slack delivery is a later phase; Phase 1
  never sends a Slack message.
  * `CitationEvent` canonical key: `citation:<short-citing-id>:<short-cited-id>`.
  * Tracked papers are read from the existing publications data source
    (`NotionClient.iter_data_source_pages`, new general-purpose paginated
    query) — only rows with a valid OpenAlex work ID become a `TrackedWork`;
    every other row is counted as skipped, never silently dropped. Never
    searches OpenAlex by researcher name.
  * OpenAlex citation retrieval (`OpenAlexClient.iter_citing_works_batch`)
    uses the official `cites` filter, OR-batched (`cites:A|B|C`, default 50
    IDs/batch, `--batch-size` overridable), cursor-paginated per batch,
    reusing the client's existing retry/backoff. Each returned citing work's
    `referenced_works` is intersected against the full tracked-ID set to
    recover every exact edge — one citing paper citing N tracked papers
    produces N `CitationEvent`s. One failed batch is recorded and never
    stops the remaining batches.
  * `--baseline` vs. incremental: `Baseline`, `Slack Status`, `Review
    Status`, `Citation Relationship`, and `Relationship Evidence` are
    create-only (see `sinks/notion_citations.py`'s update-properties
    builder) — a resync (baseline or incremental) of an *existing* citation
    key never touches them, which is what structurally guarantees a later
    baseline run can never flip an existing non-baseline row back to
    `Baseline = true`, and that human review progress and later-phase
    classification are never reset.
  * `--fixture-file` replaces both the tracked-works read and the OpenAlex
    read for fully offline runs; `--write-notion` vs. the safe default
    (dry-run preview, matching `generate_drafts.py`'s convention) is
    orthogonal to it.
  * `citation_relationship`/`relationship_evidence` default to
    `"Unclassified"`/empty — the classification step described under
    "Scholarly citation pipeline" above is not part of Phase 1.

## Completed milestones (continued, 5)

* **Citation Slack delivery** (`scripts/send_citation_notifications.py`,
  `src/arise_radar/notifications/citations.py`). Reads pending rows from
  the Citation Events Notion data source, posts each to the ARISE Slack
  signals channel (`SLACK_SIGNALS_CHANNEL_ID`), and records the delivery
  result back onto the same row. Never reads the publications data source
  or posts to the papers channel. This is delivery only — no
  citation-relationship classification, and no Slack delivery for
  publications or media items (still not built; see "Current milestone"
  below).
  * Candidate selection is a single bulk paginated read
    (`NotionClient.iter_data_source_pages`, no per-row query), scanned and
    partitioned in memory (`scan_citation_rows`): a row is only ever a
    candidate when `Baseline = false` and `Slack Status` is `Pending` (or,
    with `--retry-failed`, also `Failed`) — `Suppressed` and `Sent` rows are
    never selected, and `Baseline = true` is a hard safety net independent
    of `Slack Status`. A Citation Key shared by more than one stored page is
    a data-integrity problem, not something to guess a winner for: every
    copy is skipped and reported in `duplicate_keys`, never posted.
  * Every Notion write goes straight to the page ID already known from the
    bulk scan — never a lookup, matching the same request-count discipline
    as `run_citations.py`'s `CitationRowIndex`.
  * On success: `Slack Status = Sent` and `Slack Timestamp` are set from
    Slack's returned `ts` — only if Slack returned both `ok: true` *and* a
    timestamp. `Review Status`, `Citation Relationship`, `Relationship
    Evidence`, and everything else on the row is left untouched.
  * On failure: `Slack Status = Failed` and a concise, prefixed note
    (`[Slack delivery] ...`) is appended to `System Notes` — the existing
    value is read from the same bulk scan and appended to, never replaced,
    so unrelated existing notes survive. The rare case where Slack accepts
    the message but the follow-up Notion write itself fails is reported as
    its own category (`notion_update_after_send`) with `Slack Status`
    deliberately left unchanged, rather than guessing `Sent` or `Failed` —
    either guess risks either hiding a real gap or causing a future
    `--retry-failed` run to double-post.
  * Safe default: no `--send` flag means dry run — every candidate is
    scanned and its rendered Slack message (text + Block Kit) is printed as
    a preview, but no Slack call and no Notion write happens. `--send` is
    required for live delivery; `--limit`, `--citation-key`, and
    `--retry-failed` narrow the candidate set the same way in both modes.
  * `--error-report PATH` writes every delivery failure and duplicate-key
    conflict as structured JSON (`citation_key`, `notion_page_id`, `stage`,
    `slack_error_code`, `status_code`, `error`).
  * `SlackClient.post_message` gained an optional `blocks` param (backward
    compatible — `scripts/test_slack_connection.py` still calls it with
    plain text only); `SlackError` gained `status_code`, mirroring
    `NotionError`.

## Completed milestones (continued, 6)

* **Citation discovery and Slack delivery wired into the daily GitHub
  Actions workflow** (`.github/workflows/research-radar.yml`). Extends the
  existing scheduled/manual `research-radar` workflow rather than adding a
  competing one — roster discovery and draft generation are unchanged. Two
  new steps run after them, in order: incremental citation discovery
  (`run_citations.py --since-days 30 --write-notion`, never `--baseline` —
  that flag is reserved for the one-time historical import, already done:
  4,512 rows with `Baseline=true`/`Slack Status=Suppressed`) and citation
  Slack delivery (`send_citation_notifications.py --send`, no
  `--retry-failed`, so a transient failure requires a deliberate manual
  rerun rather than an automatic repost).
  * Both new steps use `python -u` and write a structured `--error-report`
    into `logs/` (created by a preceding `mkdir -p logs` step, since `logs/`
    is untracked and doesn't exist in a fresh checkout); both reports are
    uploaded as a `citation-error-reports` artifact by a final step with
    `if: always()` (`if-no-files-found: warn`), so record-level errors and
    duplicate-key conflicts stay inspectable even when a step fails.
  * The Slack-delivery step has no explicit `if:` — GitHub Actions' default
    "only run if the previous step succeeded" is what's relied on. This is
    safe specifically because `run_citations.py` already exits 0 for
    record-level/per-row errors (still writing its error report) and only
    exits nonzero for genuinely fatal failures (bad config, Notion
    unreachable) — so a catastrophic discovery failure correctly skips
    Slack delivery, while ordinary per-row errors don't.
  * Secrets are scoped per step, not broadened at the job level: the
    existing job-level `env` (`NOTION_TOKEN`, `NOTION_DATA_SOURCE_ID`,
    `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`) is untouched; the citation
    discovery step adds only `NOTION_CITATIONS_DATA_SOURCE_ID` at step
    level; the Slack-delivery step declares its own complete, step-level env
    (`NOTION_TOKEN`, `NOTION_CITATIONS_DATA_SOURCE_ID`, `SLACK_BOT_TOKEN`,
    `SLACK_SIGNALS_CHANNEL_ID`) — no `SLACK_PAPERS_CHANNEL_ID`, no
    `ANTHROPIC_*`, no `NOTION_DATA_SOURCE_ID`.
  * No new concurrency block: the existing repository-level `research-radar`
    group (`cancel-in-progress: false`) already covers the whole job,
    including the new citation steps, so two scheduled/manual runs still
    can't write the same citation events at once and an in-progress write
    is never killed mid-way. `workflow_dispatch` is unchanged.
  * No new workflow-validation test utility was added: none existed to
    update, and the repository has no established pattern of testing GitHub
    Actions YAML structure — adding one now would be exactly the kind of
    formatting-brittle test the project avoids elsewhere.

## Completed milestones (continued, 7)

* **New-publication Slack delivery** (`scripts/send_publication_notifications.py`,
  `src/arise_radar/notifications/publications.py`), plus the schema/backfill
  work that makes it safe to turn on without spamming the papers Slack
  channel with the existing publication backlog. Mirrors the citation Slack
  pipeline's design exactly (bulk read, in-memory selection, update by known
  page ID, dry-run-by-default) — see "Completed milestones (continued, 5)".
  * **Schema**: four new publications-data-source properties (`Slack
    Status` select — `Pending`/`Suppressed`/`Sent`/`Failed`; `Slack
    Timestamp` and `Slack Error` rich_text; `Slack Notified Date` date)
    added to `scripts/update_notion_schema.py`'s existing
    `desired_properties()` — the same additive, idempotent,
    type-conflict-refusing migration framework already used for the
    draft-generation properties, not a second competing one.
  * **Historical backfill** (`scripts/backfill_publication_slack_status.py`):
    a one-time, safely-rerunnable command that sets `Slack Status =
    Suppressed` on every row that doesn't have a Slack Status yet — and
    only those; a row with any existing value (including a value from a
    previous backfill run) is left completely untouched, which is what
    makes rerunning it a no-op. Bulk-reads the data source once; dry-run by
    default, `--write-notion` required to apply.
  * **Create-time policy** (`sinks/notion.py`'s `upsert_publication`): a
    genuinely new publication row now gets `Slack Status = Pending` at
    creation — *unless* it's non-standard (see `work_types.py`) or a
    probable version/preprint duplicate (see `version_family.py`), in which
    case it gets `Suppressed` instead. This reuses the exact same
    `hold_reasons` eligibility computation that already decides whether a
    new row defaults to `Draft Status = Needs Attention` — one policy, two
    consequences, not a second parallel eligibility rule. An existing row's
    resync (metadata, drafts, relevance, researcher names) never mentions
    `Slack Status` in its write payload, so it's always preserved exactly
    as-is — this is also what keeps a historical row from ever becoming
    `Pending` merely because roster sync touches it again before the
    backfill has run.
  * **Notifier** (`notifications/publications.py`): candidates are
    `Slack Status = Pending` (plus `Failed` with `--retry-failed`) rows
    only — `Suppressed`, empty/not-yet-backfilled, and `Sent` rows are
    never selected, and a Canonical Key shared by more than one stored page
    skips every copy and is reported, never guessed at. On success: `Slack
    Status = Sent`, `Slack Timestamp` set from Slack's returned `ts`, `Slack
    Notified Date` set to the current UTC date/time, `Slack Error` cleared
    — only if Slack returned `ok: true` *and* a timestamp. On failure:
    `Slack Status = Failed` and a concise, token-safe error saved into
    `Slack Error` (overwritten, not appended — unlike the citation
    pipeline's System Notes, there's a dedicated field for this). The rare
    case where Slack accepts the message but the follow-up Notion write
    itself fails is its own category (`notion_update_after_send`) with
    `Slack Status` deliberately left unchanged, same reasoning as the
    citation notifier. Message rendering: title, researchers, `Published
    in` (falls back to the `Source` property, e.g. "OpenAlex" — this data
    source has no dedicated Venue property and none was added), published
    date, and a `Why It Matters` → `Internal Summary` → safe-fallback chain
    are always shown; `Key Story Angle` and a Draft Social Post preview
    (only when present *and* ≤280 characters) are shown when available.
    Complete plain-text fallback plus Slack blocks, matching the citation
    notifier's pattern.
  * `--error-report PATH` writes every delivery failure and duplicate-key
    conflict as structured JSON (`canonical_key`, `notion_page_id`,
    `stage`, `slack_error_code`, `status_code`, `error`).
  * The citation Slack pipeline (`notifications/citations.py`,
    `sinks/notion_citations.py`, `scripts/run_citations.py`,
    `scripts/send_citation_notifications.py`) was not touched by this
    milestone. GitHub Actions wiring followed later — see "Completed
    milestones (continued, 8)".

## Completed milestones (continued, 8)

* **New-publication Slack delivery wired into the daily GitHub Actions
  workflow** (`.github/workflows/research-radar.yml`). Extends the same
  `research-radar` workflow that already runs citation discovery/Slack
  delivery (see "Completed milestones (continued, 6)") — no second
  scheduled workflow. One new step, inserted between drafting and citation
  discovery: `send_publication_notifications.py --send --error-report
  logs/publication_slack_errors.json`, no `--retry-failed` (a transient
  failure needs a deliberate manual rerun, not a silent automatic repost —
  same convention as the citation-delivery step).
  * Final step order: roster/publication discovery → publication drafting →
    **publication Slack delivery** → incremental citation discovery →
    citation Slack delivery → upload error-report artifacts.
  * No new `mkdir -p logs` step needed — the existing "Create logs
    directory" step (already positioned right after drafting, originally
    added for the citation steps) now also covers the publication step's
    `--error-report` output; its comment was updated to say so.
  * Secrets stay scoped per step: `NOTION_TOKEN`/`NOTION_DATA_SOURCE_ID`
    already come from the existing job-level `env`, so only
    `SLACK_BOT_TOKEN` and `SLACK_PAPERS_CHANNEL_ID` are added at step
    level — no `ANTHROPIC_*`, no `NOTION_CITATIONS_DATA_SOURCE_ID`, no
    `SLACK_SIGNALS_CHANNEL_ID`.
  * No explicit `if:` on the new step — GitHub Actions' default "only run
    if the previous step succeeded" is what keeps it from running after a
    catastrophic publication-discovery/drafting failure, the same reasoning
    already established for the citation-delivery step: a fatal failure
    exits nonzero and skips it, while a completed run with per-row errors
    still exits 0 (those errors land in the JSON report instead).
  * The artifact-upload step was extended (still `if: always()`) to include
    `logs/publication_slack_errors.json` alongside the two citation report
    files, and renamed from `citation-error-reports` to
    `notification-error-reports` since it's no longer citation-only.
  * No changes to `on:` (`workflow_dispatch` + daily `schedule`), the
    `research-radar` concurrency group, or any existing step's command.

## Current milestone

Paper summarization and ARISE-style LinkedIn draft generation
(`scripts/generate_drafts.py`).

Build only:

* select eligible Notion rows (`Status = New`, `Source = OpenAlex`, `Draft
  Status` empty/"Not Started", or any non-"Approved" status with `--force`;
  `Draft Status = Approved` is never overwritten, with or without `--force`);
* re-fetch full OpenAlex metadata + abstract (`OpenAlexClient.get_work`,
  `reconstruct_abstract` decoding `abstract_inverted_index`) for each
  selected row;
* call the Anthropic API (official `anthropic` SDK, `claude-opus-5`,
  structured output via `client.messages.parse`) for exactly 5 fields:
  `internal_summary`, `key_story_angle`, `why_it_matters`,
  `draft_social_post`, `limitations`; `source_basis` (`OpenAlex Abstract` vs
  `Metadata Only`) and `model` are recorded by the pipeline itself, not
  asked of the model — they're pipeline facts, not judgments;
* the ARISE LinkedIn example is passed as a style reference only, with an
  explicit instruction never to reuse its facts, numbers, or claims — every
  factual detail in the draft must come from that paper's own source
  material;
* on generation failure (including a safety `refusal`), retain the item,
  leave content fields untouched, store the error in `Draft Error`, set
  `Draft Status = Needs Attention` — never silently discard;
* only draft-* properties are ever written — `Status`, `Researchers`,
  `Editorial Notes`, and publication metadata are never mentioned in the
  write payload, so they're preserved by construction;
* `--limit` (default 3), `--canonical-key`, `--dry-run`, `--write-notion`,
  `--force`; every mode actually calls Anthropic and generates real
  content — `--write-notion` only gates whether it's persisted;
* mocked tests for all three external services (no live Notion, OpenAlex,
  or Anthropic calls in tests).

Do not currently build:

* citation-relationship classification (applies/extends/validates/critiques/
  mentions) — citation monitoring Phase 1 (detection + idempotent Notion
  storage only) is built; see "Completed milestones (continued, 4)" above.
  Slack delivery *for citation events* is now built (see "Completed
  milestones (continued, 5)"), but Slack delivery for publications or media
  items is not,
* media monitoring,
* broad fuzzy deduplication,
* automatic publishing,
* human approval workflow beyond the existing `Draft Status` select (still
  manual, in Notion),
* or writes to any production Notion database (still targets the local
  test data source only).

## Current success criterion

Given one verified OpenAlex author ID, this command should print that researcher’s correct recent publications:

```bash
python scripts/run_roster.py \
  --roster config/roster.yaml \
  --days-back 90
```

The output should include:

* researcher name,
* publication title,
* publication date,
* DOI when available,
* OpenAlex work ID,
* and canonical key.

With a configured test Notion data source, these commands should also work
without ever writing until `--write-notion` is passed:

```bash
python scripts/run_roster.py --roster config/roster.yaml --days-back 730 --notion-dry-run
python scripts/run_roster.py --roster config/roster.yaml --days-back 730 --write-notion
```

The schema migration should be safe to run against that same data source at
any time, including repeatedly:

```bash
python scripts/update_notion_schema.py --dry-run
python scripts/update_notion_schema.py
```

Tests must not make live network requests.

## Working instructions for Claude Code

Before making substantial changes:

1. Read this entire file.
2. Inspect the existing repository.
3. State a concise implementation plan.
4. Keep the work limited to the current milestone.
5. Do not add unrelated frameworks or abstractions.
6. Prefer small, typed, testable modules.
7. Run tests and linting after changes.
8. Report files changed and commands run.
9. Surface unresolved assumptions clearly.
10. Update this file only when the project scope or canonical architecture changes.

When instructions in a prompt conflict with this file, ask whether the project specification has changed before altering settled architecture.

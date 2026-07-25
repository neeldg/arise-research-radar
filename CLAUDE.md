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

## Current milestone

Local test Notion sink for the roster-publication pipeline.

Build only:

* Notion configuration (`NOTION_TOKEN`, `NOTION_DATA_SOURCE_ID`) loaded from
  environment variables, `.env` supported via `python-dotenv`; the token is
  never printed or logged;
* a typed Notion sink (`src/arise_radar/sinks/notion.py`) using a small
  injectable HTTP client, mirroring the OpenAlex adapter's pattern, so tests
  mock it via `httpx.MockTransport` rather than depending on the official
  `notion-client` SDK's support for the newer "data source" API surface;
* canonical-key lookup and create-or-update (upsert) behavior: no match
  creates a page with `Status = New`; exactly one match updates metadata,
  preserves the existing editorial `Status`, and merges the current
  researcher into `Researchers`; more than one match is left untouched and
  logged as a visible duplicate-key error for human repair;
* publications the relevance filter marked `exclude` are never written to
  Notion;
* a Notion failure on one publication is reported and does not stop the
  rest of the run;
* explicit CLI opt-in flags (`--write-notion`, `--notion-dry-run`, mutually
  exclusive) — the default remains fully read-only, no Notion request of
  any kind;
* mocked tests (no live Notion or OpenAlex calls);
* readable run summaries.

Do not currently build:

* Anthropic or other LLM calls,
* internal summaries or social-post drafting,
* citation monitoring,
* media monitoring,
* GitHub Actions,
* broad fuzzy deduplication,
* automatic publishing,
* or writes to any production Notion database (this milestone targets a
  local test data source only).

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

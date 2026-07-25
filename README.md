# ARISE Research Radar

Deterministic research and media-monitoring pipeline for ARISE AI. See
[`CLAUDE.md`](./CLAUDE.md) for the full project specification and current
milestone scope.

This milestone extends the **roster-publication dry run** with an optional
Notion sink: given a YAML roster of ARISE-affiliated researchers with
verified OpenAlex author IDs, fetch their recent publications, apply the
relevance filter, and — only when explicitly asked — create or update rows
in a Notion data source.

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Roster files

- `config/roster_seed.yaml` — committed. Names-only list of every person
  currently on the [ARISE team page](https://arise-ai.org/team), each with
  `openalex_id: null` and `verification_status: unverified`. This is a
  worklist, not something the CLI queries.
- `config/roster.example.yaml` — committed. Documents the production schema
  with one placeholder entry (`A0000000000` is not a real OpenAlex ID).
- `config/roster.yaml` — **version-controlled**. The real, verified roster
  the CLI actually queries. ARISE researcher names and OpenAlex IDs are
  public configuration, so roster changes are tracked and auditable via git
  history.
- `config/roster.local.yaml` — **gitignored**. Optional local/private
  override, for cases where a roster variant shouldn't be committed. Not
  currently read by the CLI; pass it explicitly via `--roster` if used.

## Adding the first verified OpenAlex author ID

1. Look up the researcher on [OpenAlex](https://openalex.org) (by name, then
   confirm via their institutional affiliation/publication list) and copy
   their author ID, e.g. `A5023888391`.
2. In `config/roster.yaml`, add an entry using the production schema:

   ```yaml
   researchers:
     - id: ethan_goh
       name: Ethan Goh
       openalex_id: A5023888391
       orcid: null
       aliases:
         - Ethan Goh
       active: true
   ```

3. Optionally update the matching entry in `config/roster_seed.yaml` to
   `verification_status: verified` for tracking purposes (the CLI ignores the
   seed file; only `openalex_id` in `roster.yaml` controls whether a
   researcher is queried).
4. Run the dry run (see below). Entries with `openalex_id: null` or
   `active: false` are skipped with a visible warning on stderr — the CLI
   never falls back to searching by name.

## Dry run

```bash
python scripts/run_roster.py --roster config/roster.yaml --days-back 90
```

## Notion sync (optional)

Reads `NOTION_TOKEN` and `NOTION_DATA_SOURCE_ID` from the environment (a
`.env` file is supported via `python-dotenv` — copy `.env.example` to `.env`
and fill in real values; `.env` is gitignored and the token is never
printed). Requires a Notion data source with these properties: `Name`
(title), `Canonical Key` (rich text), `Status` (select), `Researchers`
(multi-select), `Published Date` (date), `Detected Date` (date), `DOI` (rich
text), `OpenAlex ID` (rich text), `Source` (select), `URL` (url),
`Relevance Status` (select), `Notes` (rich text).

By default no Notion request is ever made. Two opt-in flags:

```bash
# Show exactly what would be created/updated, with no write requests:
python scripts/run_roster.py --roster config/roster.yaml --days-back 730 --notion-dry-run

# Actually create/update rows:
python scripts/run_roster.py --roster config/roster.yaml --days-back 730 --write-notion
```

Upsert behavior: publications are matched by exact `Canonical Key`. No
existing page creates one with `Status = New`; one existing page updates
metadata and merges the current researcher into `Researchers` while
preserving the existing editorial `Status`; more than one existing page with
the same key is left untouched and flagged as a duplicate needing human
repair. Publications the relevance filter marked `exclude` are never sent to
Notion. A Notion failure on one publication is reported and does not stop
the rest of the run.

## Scheduled runs (GitHub Actions)

`.github/workflows/research-radar.yml` runs the full pipeline — publication
discovery, Notion upsert, then summary/draft generation — once a day, and
can also be triggered manually from the Actions tab (`workflow_dispatch`).

### Required repository secrets

Configure these under **Settings → Secrets and variables → Actions**. CI
never reads or commits `.env` — it is gitignored and only used for local
development; secrets are injected as environment variables from GitHub's
secret store instead:

- `NOTION_TOKEN`
- `NOTION_DATA_SOURCE_ID`
- `ANTHROPIC_API_KEY`
- `ANTHROPIC_MODEL`

### What each run does

1. Installs the package (`pip install -e ".[dev]"`), then runs
   `ruff check .`, `ruff format --check .`, and `pytest`. A lint or test
   failure stops the run before anything touches Notion or Anthropic.
2. `scripts/run_roster.py --roster config/roster.yaml --days-back 14
   --write-notion`. The 14-day lookback intentionally overlaps every
   previous run — Notion's canonical-key upsert (see above) makes reruns
   idempotent, so the overlap adds resilience to a missed or failed run
   rather than creating duplicates.
3. `scripts/generate_drafts.py --limit 10 --write-notion`. Generates
   internal summaries and draft social posts for up to 10 eligible rows and
   writes them to Notion as drafts only. This workflow never approves,
   schedules, or publishes anything — every item still requires human
   review in Notion.

### Operational notes

- The schedule runs at 16:00 UTC. GitHub Actions cron is always UTC and
  does not shift for daylight saving, so this is 8:00am
  America/Los_Angeles (PST, Nov-Mar) / 9:00am America/Los_Angeles (PDT,
  Mar-Nov).
- A single `concurrency` group (`research-radar`) queues a new run behind
  an in-progress one instead of cancelling it, so two runs can never write
  to Notion at the same time.
- The job has a 30-minute timeout.
- Any failure — lint, test, OpenAlex, Notion, or Anthropic — fails the job
  and is visible in the Actions run log and status; nothing is swallowed.
- Secrets are only ever referenced via `${{ secrets.* }}` / environment
  variables; no step prints them.

## Testing and linting

```bash
ruff check .
ruff format --check .
pytest
```

Tests never make live network requests — the OpenAlex and Notion clients are
both injectable and tests back them with `httpx.MockTransport`.

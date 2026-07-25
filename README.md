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

## Testing and linting

```bash
ruff check .
ruff format --check .
pytest
```

Tests never make live network requests — the OpenAlex and Notion clients are
both injectable and tests back them with `httpx.MockTransport`.

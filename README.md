# ARISE Research Radar

Deterministic research and media-monitoring pipeline for ARISE AI. See
[`CLAUDE.md`](./CLAUDE.md) for the full project specification and current
milestone scope.

This milestone implements only the **roster-publication dry run**: given a
YAML roster of ARISE-affiliated researchers with verified OpenAlex author
IDs, fetch and print their recent publications.

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
- `config/roster.yaml` — **gitignored**. The real, verified roster the CLI
  actually queries. Starts as `researchers: []`.

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

## Testing and linting

```bash
ruff check .
ruff format --check .
pytest
```

Tests never make live network requests — the OpenAlex client is injectable
and tests back it with `httpx.MockTransport`.

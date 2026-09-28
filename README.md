# Awardline procurement agent example

A runnable competitor watcher for UK Contracts Finder awards, plus the exact buyer-history action it calls over HTTP and MCP.

## Run it in one command

Python 3.12+ is required. Install this repository with `pip install -e ".[mcp]"`, then run:

```bash
awardline-watcher --demo
```

The demo reads **synthetic** award releases, notices that supplier ID `GB-COH-456` won an award in CPV category `7220`, and calls the actual FastAPI buyer-history route through an in-process HTTP transport. It prints the exact trigger, why it called the action, and the bounded evidence response. The synthetic links use `example.invalid`. No database, internet connection, account, wallet or payment is required for this run.

## What the agent calls

`GET /v1/buyer-history?buyer_id=<published ID>&category=<four-digit CPV>&limit=20` returns exact-ID award history: published buyer and supplier IDs, award dates, values, published contract ends, source references, a revision and coverage limits. Some source-scoped buyer IDs contain `/`, so the ID is a query parameter. The same action is exposed as the single MCP tool `awardline_buyer_history(buyer_id, category, limit=20)` through stdio or localhost Streamable HTTP. See the [example guide](examples/competitor-watcher/README.md) for launch commands and a live-source watcher run.

Relationships use published IDs and exact CPV prefixes. The code does not infer legal-entity merges, incumbency, renewal probability or future tenders. Later award cancellations and CPV corrections retire earlier hits. Responses are capped at 20 awards and disclose when truncated.

## Status

This is a **no-charge local research build**, not a hosted API. Full buyer-history reads are disabled by default and require the explicit localhost `--research-demo` flag. There is no x402 settlement or billable mode. The legacy active-tender pilot modules remain in the repository, but the one-tool MCP server in `awardline/history_mcp.py` is the current product interface. Running the live watcher requires a PostgreSQL database with recent Contracts Finder ingestion; the demo does not.

Apply `sql/001_ingestion.sql` through `sql/004_pilot_terms.sql` to an isolated UTF-8 PostgreSQL database before using the local API with source data. Set `AWARDLINE_DATABASE_URL`, then run `python -m awardline.server --research-demo` for HTTP or `python -m awardline.history_mcp --research-demo` for stdio MCP. `python -m awardline.history_mcp --transport streamable-http --port 8001 --research-demo` serves MCP at `http://127.0.0.1:8001/mcp`, bound to localhost. Payment and public hosting require further work.

The test suite runs with `python -m pytest -q`; set `AWARDLINE_TEST_DATABASE_URL` to a database named `awardline_test*` for PostgreSQL integration tests. This release passed 101 tests against a local UTF-8 PostgreSQL test database, including an MCP Streamable HTTP smoke test against a clean 30-day source sample.

Contracts Finder data is [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) material. Attribute the Cabinet Office / Contracts Finder in downstream use. The application code is MIT licensed.

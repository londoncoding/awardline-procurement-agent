# Competitor-watching agent

This reference agent watches **published supplier IDs**, not supplier names. When a new active Contracts Finder award in the selected four-digit CPV category names that exact supplier ID, it calls Awardline's buyer-history action and prints the source-backed result and the reason for the call. It never predicts a renewal or calls a supplier an incumbent.

## One-command demo

From the repository root, after installing `pip install -e .`:

```bash
awardline-watcher --demo
```

Or run `python -m awardline.competitor_watcher --demo`. This exercises the real FastAPI buyer-history route through an in-process HTTP transport. Its two award releases are **synthetic examples**; it needs no database, network, account, wallet or payment. The output has one trigger, the exact supplier and buyer IDs, the reason for the call, a bounded award-history result, and evidence URLs. The demo URLs are illustrative and must not be treated as real procurement records.

## Local source-backed run

Keep a Contracts Finder ingestion database current, set `AWARDLINE_DATABASE_URL`, and start the API on localhost with `python -m awardline.server --research-demo`. In another terminal:

```bash
awardline-watcher --live --supplier-id GB-COH-05906258 --category 8000 --api-base-url http://127.0.0.1:8000
```

Use a supplier ID and CPV category relevant to your watch. The example polls a fixed last-24-hours publication window from the free Contracts Finder OCDS search endpoint. It stops if a source page is malformed or the window exceeds ten pages; split the window instead of silently dropping events. It stores observed award IDs in `.awardline-watcher-state.json` so a later run does not repeat the same call. `--max-history-calls` defaults to **one** per run. The API must contain ingested history for the buyer; a new source event may be visible to the watcher before the local database ingests it.

The full buyer-history action is `GET /v1/buyer-history?buyer_id=<published ID>&category=<four-digit CPV>&limit=20`. A free `GET /v1/buyer-history/preview` with the same buyer/category query gives the count, date range, result cap and revision without supplier IDs or values. IDs stay in the query string because some published IDs contain `/`. The full result includes exact published supplier IDs, dates, values, contract end dates when published, source links and bounded-coverage notes. The full endpoint returns 503 unless the server is explicitly started with `--research-demo`. There is no x402 checkout or billable mode yet.

## MCP access to the same action

Install `pip install -e ".[mcp]"`, set `AWARDLINE_DATABASE_URL`, then run:

```bash
python -m awardline.history_mcp --research-demo
```

This stdio server exposes `awardline_buyer_history_preview(buyer_id, category)` and `awardline_buyer_history(buyer_id, category, limit=20)`. For a **localhost-only** Streamable HTTP MCP endpoint, use `python -m awardline.history_mcp --transport streamable-http --port 8001 --research-demo`; the MCP URL is `http://127.0.0.1:8001/mcp`. Both MCP transports call the same FastAPI handlers as ordinary HTTP. A hosted, authenticated endpoint and payment settlement are later launch gates.

Contracts Finder data is [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) material. Attribute the Cabinet Office / Contracts Finder in downstream use.

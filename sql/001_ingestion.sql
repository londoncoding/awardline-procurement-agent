CREATE TABLE IF NOT EXISTS ingest_windows (
    id uuid PRIMARY KEY,
    source text NOT NULL,
    published_from timestamptz NOT NULL,
    published_to timestamptz NOT NULL,
    next_url text NOT NULL,
    state text NOT NULL DEFAULT 'running' CHECK (state IN ('running', 'complete', 'complete_with_issues')),
    pages_seen integer NOT NULL DEFAULT 0,
    releases_seen integer NOT NULL DEFAULT 0,
    issues_seen integer NOT NULL DEFAULT 0,
    cooldown_until timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ingest_window_bounds CHECK (published_from < published_to),
    CONSTRAINT unique_ingest_window UNIQUE (source, published_from, published_to)
);

CREATE TABLE IF NOT EXISTS raw_pages (
    id uuid PRIMARY KEY,
    window_id uuid NOT NULL REFERENCES ingest_windows(id),
    page_url text NOT NULL,
    next_url text,
    package jsonb NOT NULL,
    response_bytes bytea NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (window_id, page_url)
);

CREATE TABLE IF NOT EXISTS source_releases (
    id uuid PRIMARY KEY,
    source text NOT NULL,
    ocid text NOT NULL,
    release_id text NOT NULL,
    content_sha256 char(64) NOT NULL,
    release jsonb NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, ocid, release_id, content_sha256)
);
CREATE INDEX IF NOT EXISTS source_releases_process_idx ON source_releases (source, ocid, first_seen_at DESC);
CREATE INDEX IF NOT EXISTS source_releases_buyer_idx ON source_releases ((release #>> '{buyer,identifier,scheme}'), (release #>> '{buyer,identifier,id}'));

CREATE TABLE IF NOT EXISTS ingest_issues (
    id uuid PRIMARY KEY,
    window_id uuid NOT NULL REFERENCES ingest_windows(id),
    page_url text NOT NULL,
    release_index integer,
    reason text NOT NULL,
    raw jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (window_id, page_url, release_index, reason)
);

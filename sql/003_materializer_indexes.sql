-- Supports a streaming process scan and exact buyer-ID award lookups after
-- historical backfill. Buyer names are intentionally not indexed for matching.
CREATE INDEX IF NOT EXISTS source_releases_process_scan_idx
    ON source_releases (source, ocid, first_seen_at, id);

CREATE INDEX IF NOT EXISTS source_releases_awards_buyer_id_idx
    ON source_releases (source, (release #>> '{buyer,id}'))
    WHERE jsonb_typeof(release->'awards') = 'array'
      AND release->'awards' <> '[]'::jsonb;

CREATE INDEX IF NOT EXISTS source_releases_awards_identifier_idx
    ON source_releases (
        source,
        (release #>> '{buyer,identifier,scheme}'),
        (release #>> '{buyer,identifier,id}')
    )
    WHERE jsonb_typeof(release->'awards') = 'array'
      AND release->'awards' <> '[]'::jsonb;

CREATE INDEX IF NOT EXISTS ingest_windows_coverage_idx
    ON ingest_windows (source, published_from, published_to)
    WHERE state = 'complete' AND issues_seen = 0;

CREATE INDEX IF NOT EXISTS dossiers_discovery_category_idx
    ON dossiers ((payload #>> '{opportunity,service_category}'), data_as_of, id)
    WHERE eligible = true;

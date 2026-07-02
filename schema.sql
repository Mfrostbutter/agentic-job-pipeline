-- Job Pipeline schema: two tables.
--
--   seen_jobs  lightweight dedup ledger of every source id ever scraped.
--              The n8n workflows check it BEFORE inserting, so re-scraped
--              postings never become duplicate rows.
--   jobs       the system of record. One row per posting; score, tier,
--              status, and review fields live here and drive the web UI.
--
-- Applied automatically on first `docker compose up` (mounted into
-- /docker-entrypoint-initdb.d/). To apply by hand:
--   psql -h <host> -U pipeline -d jobpipeline -f schema.sql   (idempotent)

CREATE TABLE IF NOT EXISTS seen_jobs (
    job_id        text PRIMARY KEY,
    source        text        NOT NULL DEFAULT '',   -- 'linkedin' | 'manual' | ...
    status        text        NOT NULL DEFAULT 'new',-- 'new' | 'reject' | 'built' | ...
    reason        text,
    last_updated  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id                       text PRIMARY KEY,
    title                        text        NOT NULL DEFAULT '',
    company                      text        NOT NULL DEFAULT '',
    location                     text        NOT NULL DEFAULT '',
    source_url                   text        NOT NULL DEFAULT '',
    workplace                    text        NOT NULL DEFAULT '',
    status                       text        NOT NULL DEFAULT 'New',
    relevance_score              numeric,
    breakdown                    text        NOT NULL DEFAULT '',
    salary_min                   numeric,
    salary_max                   numeric,
    description                  text        NOT NULL DEFAULT '',
    tier                         text        NOT NULL DEFAULT '',
    ai_reviewed                  boolean     NOT NULL DEFAULT false,
    build_package                boolean     NOT NULL DEFAULT false,
    application_package_created  boolean     NOT NULL DEFAULT false,
    package_path                 text        NOT NULL DEFAULT '',
    run_label                    text        NOT NULL DEFAULT '',
    stage                        text        NOT NULL DEFAULT '',  -- application funnel: Applied/Heard Back/Screen/Interview/Offer/Rejected/Ghosted
    stage_updated_at             timestamptz,
    ai_override                  boolean     NOT NULL DEFAULT false,
    override_reason              text        NOT NULL DEFAULT '',
    created_at                   timestamptz NOT NULL DEFAULT now(),
    updated_at                   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jobs_tier_idx    ON jobs (tier);
CREATE INDEX IF NOT EXISTS jobs_status_idx  ON jobs (status);
CREATE INDEX IF NOT EXISTS jobs_created_idx ON jobs (created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_triage_idx  ON jobs (tier, ai_reviewed);
CREATE INDEX IF NOT EXISTS jobs_stage_idx   ON jobs (stage);

-- Keep updated_at honest regardless of which writer touches the row.
CREATE OR REPLACE FUNCTION jobs_touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS jobs_set_updated_at ON jobs;
CREATE TRIGGER jobs_set_updated_at
    BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION jobs_touch_updated_at();

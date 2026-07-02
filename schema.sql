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


-- ===========================================================================
-- Recruiter mode ("aging listings") tables.
--
--   seen_leads  dedup ledger AND persistence signal. Unlike seen_jobs it also
--               tracks first_seen / last_seen / times_seen, so a posting that
--               keeps reappearing across scans is a proven long-open lead even
--               when LinkedIn's date filter hides the true posting age.
--   leads       system of record for recruiter leads. One row per aging
--               posting; lead score, posting age, campaign, outreach drafts,
--               and pipeline stage live here and drive the recruiter UI.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS seen_leads (
    lead_id       text PRIMARY KEY,
    source        text        NOT NULL DEFAULT '',    -- 'linkedin' | 'manual' | ...
    status        text        NOT NULL DEFAULT 'new',
    reason        text,
    first_seen    timestamptz NOT NULL DEFAULT now(),
    last_seen     timestamptz NOT NULL DEFAULT now(),
    times_seen    integer     NOT NULL DEFAULT 1       -- bumped every re-sight
);

CREATE TABLE IF NOT EXISTS leads (
    lead_id                 text PRIMARY KEY,
    role_title              text        NOT NULL DEFAULT '',
    company                 text        NOT NULL DEFAULT '',
    location                text        NOT NULL DEFAULT '',
    source_url              text        NOT NULL DEFAULT '',
    workplace               text        NOT NULL DEFAULT '',
    status                  text        NOT NULL DEFAULT 'New',
    lead_score              numeric,
    breakdown               text        NOT NULL DEFAULT '',
    posting_age_days        numeric,
    campaign_label          text        NOT NULL DEFAULT '',
    description             text        NOT NULL DEFAULT '',
    tier                    text        NOT NULL DEFAULT '',   -- Top Tier | Second Choice | Reject (from AI review)
    angle                   text        NOT NULL DEFAULT '',   -- one-line outreach angle from the reviewer
    ai_reviewed             boolean     NOT NULL DEFAULT false,
    build_outreach          boolean     NOT NULL DEFAULT false,-- the manual cost gate
    outreach_created        boolean     NOT NULL DEFAULT false,
    outreach_linkedin       text        NOT NULL DEFAULT '',
    outreach_email_subject  text        NOT NULL DEFAULT '',
    outreach_email_body     text        NOT NULL DEFAULT '',
    contact_name            text        NOT NULL DEFAULT '',   -- recruiter fills; scrape has no person
    contact_handle          text        NOT NULL DEFAULT '',
    package_path            text        NOT NULL DEFAULT '',   -- staged outreach files on disk
    stage                   text        NOT NULL DEFAULT '',   -- pipeline: Contacted/Replied/Meeting/Won/Lost
    stage_updated_at        timestamptz,
    ai_override             boolean     NOT NULL DEFAULT false,
    override_reason         text        NOT NULL DEFAULT '',
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS leads_tier_idx     ON leads (tier);
CREATE INDEX IF NOT EXISTS leads_status_idx   ON leads (status);
CREATE INDEX IF NOT EXISTS leads_campaign_idx ON leads (campaign_label);
CREATE INDEX IF NOT EXISTS leads_age_idx      ON leads (posting_age_days DESC);
CREATE INDEX IF NOT EXISTS leads_created_idx  ON leads (created_at DESC);

-- Reuse jobs_touch_updated_at() (same body) for the leads updated_at trigger.
DROP TRIGGER IF EXISTS leads_set_updated_at ON leads;
CREATE TRIGGER leads_set_updated_at
    BEFORE UPDATE ON leads
    FOR EACH ROW EXECUTE FUNCTION jobs_touch_updated_at();

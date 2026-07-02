# Recruiter mode: the same engine, pointed the other way

This pipeline is a scrape, score, human-review, agent-draft engine. Nothing in
it is inherently "for job hunting." Point it one way and it is a job-search tool.
Point it the other way and it is a sourcing and warm-outreach tool for a
recruiter, staffing desk, or fractional/consulting practice.

There are two ways to run it as a recruiter. Pick based on how far you want to go.

- **Option A (quick): repurpose job-search mode.** Change what you scrape, how
  you score, and what the writer drafts. Covered at the bottom under
  "Repurposing job-search mode."
- **Option B (dedicated): aging-listings mode.** A purpose-built workflow +
  schema + agents that treat a long-open posting as the buying signal. This is
  what the rest of this doc describes.

## The premise: aging listings are demand signals

A fresh posting tells you a company is hiring. A posting that has been open for
weeks tells you a company is *struggling* to hire, which is exactly when a
recruiter is worth a conversation. Aging-listings mode scrapes wide, drops
anything too fresh, and ranks what remains by how long it has been open.

## The pieces (parallel to job-search mode)

| Job-search mode | Recruiter (aging-listings) mode |
|---|---|
| `n8n/workflow-custom-form-scrape.json` | `n8n/workflow-recruiter-aging-listings.json` |
| `form/index.html` | `form/recruiter.html` |
| `jobs` + `seen_jobs` tables | `leads` + `seen_leads` tables |
| `pipeline/review_new_jobs.py` (Haiku triage) | `pipeline/review_leads.py` (Haiku lead triage) |
| `pipeline/build_packages.py` (Sonnet resume/cover) | `pipeline/outreach_packages.py` (Sonnet LinkedIn + email) |
| `POST /jobs/review`, `POST /jobs/build` | `POST /leads/review`, `POST /leads/outreach` |

All of it is already in the repo. The database schema in `schema.sql` creates the
`leads` and `seen_leads` tables alongside the job tables, so `docker compose up`
provisions both.

## How it flows

1. **Scan.** A recruiter submits `form/recruiter.html` (niche roles, territory,
   minimum posting age, exclude-staffing toggle, campaign label). No baked-in
   config. The workflow scrapes the widest date window on purpose.
2. **Score.** The `Dedup and Score` node drops postings younger than the
   staleness floor and any staffing/recruiting firms, then scores what remains:
   staleness is the primary signal (older = higher), plus niche title match,
   hard-to-fill seniority, hiring-intent language in the JD, and company size.
3. **Store + persist.** New leads land in `leads`. `seen_leads` is both the dedup
   ledger and the persistence signal: it tracks `first_seen`, `last_seen`, and
   `times_seen`, so a role that keeps reappearing across scans is a proven
   long-open lead even when LinkedIn's date filter hides the true age.
4. **Review (automatic, cheap).** The workflow calls `POST /leads/review`. A
   Haiku agent tiers each new lead (Top Tier / Second Choice / Reject) as a
   *prospect* and writes a one-line outreach angle, grounded in your desk digest.
5. **Outreach (manual, gated).** You flag the leads worth pursuing
   (`build_outreach`), then trigger `POST /leads/outreach`. A Sonnet agent drafts
   a short LinkedIn message and a cold email per lead, referencing the specific
   long-open role, and writes them back to the `leads` row. The human reviews and
   sends. Nothing auto-sends.

The manual gate is the same cost-control idea as job-search mode: scraping and
triage are cheap and run automatically; the expensive drafting step only runs on
leads you chose.

## Grounding: your desk

Outreach quality lives entirely in the `profile/` directory, the same as
job-search mode.

- `profile/profile.yaml` -> set `candidate.name` to your name, `candidate.company`
  to your firm, and use `search.target_titles` / `search.location_policy` /
  `search.title_guidance` to describe your desk.
- `profile/profile.md` -> your **desk digest**: who you place, real placements
  with numbers, your differentiators, your terms. The agents can only claim what
  is in this file, which keeps outreach honest. See
  `profile/recruiter-desk.example.md` for a filled-in example. If you leave the
  example files in place, the pipeline still runs but drafts generic copy for
  "Alex Example."

## Where drafts are staged (CRM / email import)

Each drafted package is written to the `leads` row **and** staged on disk under
`OUTREACH_DIR` (default `<repo>/out/outreach/<campaign>/`):

- `<company-role>/linkedin.txt` and `<company-role>/email.txt` per lead
- `outreach.csv` and `outreach.json` per campaign, with `lead_id`, company, role,
  URL, posting age, lead score, the LinkedIn message, and the email subject/body

The CSV/JSON are deliberately provider-agnostic so they import into whatever email
tool or CRM you use. Nothing in the pipeline sends on your behalf.

## Setup

1. Import `n8n/workflow-recruiter-aging-listings.json` into n8n and attach the
   four credentials (Postgres, Apify header auth, API bearer, webhook auth), the
   same ones the job workflows use. See `docs/SETUP.md`.
2. Point `form/recruiter.html` at your n8n base URL and paste the bearer token.
3. Run a scan, let review run, flag leads in the web UI, then trigger outreach.

## Command-line use

Both scripts run standalone with `--json` (that is how the service calls them):

```bash
cd pipeline
python review_leads.py --all-pending --json      # Haiku triage of the lead queue
python outreach_packages.py --limit 5 --json     # Sonnet drafts for flagged leads
```

---

## Repurposing job-search mode (Option A)

If you would rather not run the dedicated mode, you can point the job-search
tools at recruiting with configuration alone:

- **Scrape** the roles your bench (or service) fills instead of roles you want.
- **Score** by how good a prospect a posting is: put buying signals
  (`urgent`, `immediate start`, `multiple positions`) in `bonusJDKeywords`, your
  territory in `homeAreaKeywords`, and a fee floor in `salaryFloor`.
- **Triage** with a `profile.yaml` whose `title_guidance` defines "can I fill or
  win this?" and whose `auto_reject_rules` drop agencies-not-welcome postings.
- **Draft** outreach by grounding `profile.md` in your track record. The critic
  still enforces grounding and banned phrases.

The dedicated mode above exists because aging-signal scoring, the persistence
ledger, and two-part outreach drafting are awkward to express purely as
job-search config. If you are serious about recruiting use, run Option B.

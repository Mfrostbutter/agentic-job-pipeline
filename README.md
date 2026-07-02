# Job Pipeline

Scrape LinkedIn jobs daily, score them against your own rubric, triage them with a Claude agent, review the survivors in a web UI, and let a second agent draft the application package for the ones you pick. Postgres holds the state, n8n runs the automation, FastAPI serves the UI and the agents.

Point it one way and it is a job-hunting machine. Swap the filters and the profile and it is a recruiter's sourcing and warm-outreach engine; see [docs/RECRUITER-MODE.md](docs/RECRUITER-MODE.md). Either way the shape is the same: **scrape, score, human review, agent draft.**

> **Setting this up with an AI assistant?** Paste [AI-SETUP-PROMPT.md](AI-SETUP-PROMPT.md) into Claude, ChatGPT, or your agent of choice and it will walk you (or itself) through the whole install.

![Job queue](https://raw.githubusercontent.com/Mfrostbutter/agentic-job-pipeline/main/docs/screenshots/queue.png)

## How it works

```
                       ┌────────────────────────── n8n ──────────────────────────┐
  schedule / webhook → │ Config → LinkedIn scrape (Apify) → normalize → dedup     │
  custom-scrape form → │ → score → Postgres (jobs + seen_jobs) → Slack digest     │
                       └───────────────┬──────────────────────────────────────────┘
                                       │ POST /jobs/review
                       ┌───────────────▼───────────── FastAPI ────────────────────┐
                       │ review_new_jobs.py  (Claude Haiku triage: Top/Second/    │
                       │   Reject, grounded in YOUR profile + rubric)             │
                       │ web UI: queue, tier overrides, build flags, editor       │
                       │ build_packages.py   (Claude Sonnet writes resume.md,     │
                       │   Haiku critic scores it, Sonnet revises weak spots,     │
                       │   WeasyPrint renders the PDF)                            │
                       └──────────────────────────────────────────────────────────┘
```

- **Two n8n workflows.** A scheduled scraper with your standing search baked into its Config node, and a form-driven scraper that takes keywords/location/filters per request (from the web UI modal or the standalone [form/index.html](form/index.html)).
- **Deterministic pre-classifier first, AI second.** Batch dedup, location rules, title regexes, and score demotions run for free before any paid call. Claude Haiku only sees what survives.
- **Human-in-the-loop by design.** Nothing gets an application package unless you flag it. Your Reject-to-Top overrides feed back into the triage prompt as few-shot corrections, so the AI learns your taste.
- **Everything personal lives in `profile/`.** The code is generic; your search config, background digest, and templates are three files you own.

The scheduled scraper, with the standing search baked into its Config node:

![Scheduled scrape workflow](https://raw.githubusercontent.com/Mfrostbutter/agentic-job-pipeline/main/docs/screenshots/workflow.png)

And the form-driven variant, where each webhook request carries its own keywords, location, and filters:

![Custom form scrape workflow](https://raw.githubusercontent.com/Mfrostbutter/agentic-job-pipeline/main/docs/screenshots/workflow-custom-form.png)

## Quickstart

Prereqs: Docker + Docker Compose, an [n8n](https://n8n.io) instance (cloud or self-hosted), an [Apify](https://apify.com) account, an [Anthropic API key](https://console.anthropic.com). Step-by-step for all of these, including "I have never made a Slack webhook": [docs/SETUP.md](docs/SETUP.md).

```bash
git clone https://github.com/Mfrostbutter/agentic-job-pipeline
cd agentic-job-pipeline

# 1. Secrets + config
cp .env.example .env            # fill in API_BEARER_TOKEN, ANTHROPIC_API_KEY, passwords

# 2. Your profile (who you are + what you're hunting)
cp profile/profile.example.yaml profile/profile.yaml
cp profile/profile.example.md   profile/profile.md
# edit both; optionally add profile/resume-template.md and resume-example.md

# 3. Up (Postgres schema applies automatically on first boot)
docker compose up -d --build

# 4. Web UI
open http://localhost:8094/jobs/ui   # paste your API_BEARER_TOKEN when prompted
```

Then import the two workflows from [n8n/](n8n/) into your n8n instance, create the four credentials, and set `SLACK_WEBHOOK_URL` in n8n's environment if you want digests. Full walkthrough in [docs/SETUP.md](docs/SETUP.md).

![Slack digest](https://raw.githubusercontent.com/Mfrostbutter/agentic-job-pipeline/main/docs/screenshots/slack-digest.png)

## Repo layout

| Path | What it is |
|---|---|
| `app/` | FastAPI service: bearer-gated API + single-file web UI (`/jobs/ui`) |
| `pipeline/` | The agents and data layer: `review_new_jobs.py`, `build_packages.py`, `jobs_repo.py`, `seen_jobs_db.py`, `render_resume.py` |
| `profile/` | YOUR search config, background digest, and resume templates (examples ship; real files are gitignored) |
| `n8n/` | Both workflow exports, sanitized and import-ready |
| `form/` | Standalone custom-scrape form (single HTML file, no server) |
| `schema.sql` | Postgres schema: `jobs` + `seen_jobs` |
| `docs/` | Setup walkthrough, recruiter mode, screenshots |

## The dual-use idea

The engine does not care what it is hunting. `profile.yaml` defines the rubric, the workflows define the searches, and the writer drafts whatever document the profile tells it to ground. Job seeker: scrape roles, score against your background, draft resumes. Recruiter or agency: scrape roles (which are hiring-need signals), score against your candidate bench or service offering, draft outreach. [docs/RECRUITER-MODE.md](docs/RECRUITER-MODE.md) shows the exact knobs.

## Costs, roughly

- Apify scrape: about $0.05 per 25 results with the default public actor ([Mfrostbutter/linkedin-jobs-scraper](https://apify.com/mfrostbutter/linkedin-jobs-scraper))
- Haiku triage: fractions of a cent per job, and the pre-classifier keeps most jobs away from the API entirely
- Sonnet package build: roughly $0.05 to $0.15 per package (writer + critic + conditional revise)

## Safety rails you get for free

- Run guard: the scheduled scraper refuses to fire twice within 45 minutes
- Hard cap of 10 Apify queries per run, enforced in two places
- Safe mode ships ON in the scheduled workflow config (3 queries, 10 results) until you flip it
- Build lock: only one package build at a time, so overlapping triggers cannot double-bill
- The service refuses all requests if no bearer token is configured

## License

[MIT](LICENSE)

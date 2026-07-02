# Setup, step by step

This walkthrough assumes nothing: no existing database, no Apify account, no Slack webhook. Budget 30 to 60 minutes for a first install. If you would rather have an AI assistant drive, paste [../AI-SETUP-PROMPT.md](../AI-SETUP-PROMPT.md) into Claude or ChatGPT and follow along.

The moving parts:

| Piece | Runs where | You get it from |
|---|---|---|
| Postgres + FastAPI app | Docker Compose in this repo | `docker compose up` |
| n8n (the scraper workflows) | n8n cloud or your own n8n | [n8n.io](https://n8n.io) |
| LinkedIn scraping | Apify cloud actor | [apify.com](https://apify.com) |
| AI triage + resume writing | Anthropic API | [console.anthropic.com](https://console.anthropic.com) |
| Run digests (optional) | Slack incoming webhook | [api.slack.com/apps](https://api.slack.com/apps) |

## 1. Accounts and keys

### Anthropic API key
1. Sign up at [console.anthropic.com](https://console.anthropic.com).
2. Add a little credit (Settings -> Billing). $5 goes a long way here.
3. API Keys -> Create Key. Copy it somewhere safe; you cannot view it again.

### Apify token
1. Sign up at [apify.com](https://apify.com) (free plan is fine to start).
2. [Console](https://console.apify.com) -> Settings -> Integrations -> Personal API tokens -> copy the default token (or create one).
3. The workflows call the public actor [Mfrostbutter/linkedin-jobs-scraper](https://apify.com/mfrostbutter/linkedin-jobs-scraper). Nothing to install; your token is billed for the compute. To use a different LinkedIn scraper actor, change the URL in the "Run Apify (HTTP Sync)" node and adjust the "Normalize LinkedIn" code node to match its output fields.

### Slack incoming webhook (optional)
1. Go to [api.slack.com/apps](https://api.slack.com/apps) -> **Create New App** -> **From scratch**. Name it anything ("job-pipeline"), pick your workspace.
2. In the app's sidebar: **Incoming Webhooks** -> toggle **On**.
3. **Add New Webhook to Workspace** -> choose the channel for digests -> **Allow**.
4. Copy the webhook URL (`https://hooks.slack.com/services/...`). Treat it like a password; anyone with the URL can post to your channel.

### n8n
Any of these works:
- **n8n cloud**: sign up at n8n.io, done.
- **Self-hosted**: `docker run -it --rm -p 5678:5678 -v n8n_data:/home/node/.n8n n8nio/n8n` or see the [n8n docs](https://docs.n8n.io/hosting/).

## 2. Postgres + app (Docker Compose)

```bash
git clone https://github.com/Mfrostbutter/agentic-job-pipeline
cd agentic-job-pipeline
cp .env.example .env
```

Edit `.env`:

```
API_BEARER_TOKEN=   # python -c "import secrets; print(secrets.token_hex(32))"
ANTHROPIC_API_KEY=  # from step 1
PG_PASSWORD=        # any strong password
POSTGRES_PASSWORD=  # SAME value as PG_PASSWORD
SLACK_WEBHOOK_URL=  # optional
```

One token to remember: `API_BEARER_TOKEN` protects the FastAPI service, the web UI, and both n8n webhooks. You will paste it into two n8n credentials in step 4 and into the web UI on first load.

Copy and personalize your profile (this is what the AI grounds every claim in):

```bash
cp profile/profile.example.yaml profile/profile.yaml   # search config: titles, salary floor, rules
cp profile/profile.example.md   profile/profile.md     # your background digest
# optional: profile/resume-template.md, profile/resume-example.md
```

Start it:

```bash
docker compose up -d --build
curl http://localhost:8094/health     # {"status":"ok","service":"job-pipeline",...}
```

The schema applies automatically the first time the Postgres volume is created. To apply by hand instead: `docker compose exec postgres psql -U pipeline -d jobpipeline -f /docker-entrypoint-initdb.d/schema.sql`.

Open http://localhost:8094/jobs/ui and paste your `API_BEARER_TOKEN` when prompted.

If n8n runs on a different machine, set `HOST_BIND_IP` in `.env` to this machine's LAN IP (default binds to 127.0.0.1 only) and re-run `docker compose up -d`.

## 3. Import the workflows

In n8n: **Workflows -> Import from File**, once for each of:

- `n8n/workflow-scheduled-scrape.json`: your standing daily search, config baked into the **Config** node
- `n8n/workflow-custom-form-scrape.json`: ad-hoc searches driven by the web UI modal or `form/index.html`

## 4. Create the n8n credentials

n8n -> Credentials -> Add credential:

1. **Postgres** named `Job Pipeline Postgres`:
   host = the machine running docker compose (its LAN IP, or `host.docker.internal` if n8n itself runs in Docker on the same machine; `localhost` only works if n8n runs directly on that machine), port `5432`, database `jobpipeline`, user `pipeline`, password = your `PG_PASSWORD`.
2. **Header Auth** named `Apify API (Header Auth)`:
   name `Authorization`, value `Bearer <your Apify token>`.
3. **Header Auth** named `Job Pipeline API Bearer`:
   name `Authorization`, value `Bearer <your API_BEARER_TOKEN>`.
4. **Header Auth** named `Job Pipeline Webhook Auth`:
   name `Authorization`, value `Bearer <your API_BEARER_TOKEN>`.

Open each imported workflow and click through any node with a credential warning, selecting the matching credential (Postgres nodes -> 1, Run Apify -> 2, Trigger Review + Build -> 3, Webhook trigger -> 4).

Environment variables for n8n (set in its environment, e.g. `docker run -e ...` or the cloud UI):

- `SLACK_WEBHOOK_URL`: your webhook from step 1 (leave unset to disable Slack).
- `REVIEW_API_URL`: where n8n reaches the app, default `http://localhost:8094`. Same reachability logic as the Postgres host above.

## 5. Configure your search and go

1. Open the scheduled workflow's **Config** code node. Everything you should touch is marked: `roleKeywords` (the titles to search), `locationPlans`, and the `scoring` block. It ships with `safeMode: true` (3 queries, 10 results each) so a test run costs pennies.
2. Execute the workflow manually from the editor. Watch rows land in the web UI queue with scores and AI tiers.
3. Happy? Set `safeMode: false`, save, and **activate** both workflows. The schedule fires daily at 07:00 in your n8n instance's timezone.
4. In the web UI: Settings (gear at the bottom of the sidebar) -> **Set n8n URL** so the scrape buttons know where to POST.

## 6. Daily loop

1. Morning digest arrives in Slack (or just open the UI).
2. **Queue**: skim the AI tiers. Disagree with a Reject? Change its tier; your reason feeds the next run's prompt as a few-shot correction.
3. Flip **Build** on the ones worth applying to.
4. **Run build queue** (hammer icon). Sonnet drafts `resume.md`, a Haiku critic scores it, weak drafts get one targeted revision, WeasyPrint renders the PDF.
5. **Built** tab: open the package, edit the markdown inline (Ctrl+S saves, Ctrl+Shift+R re-renders the PDF), download, apply.
6. Mark it **Applied** and track the funnel stage (Heard Back, Screen, Interview, Offer) on the Applied tab.

Packages land on disk under `data/applications/<date>-<company>-<role>/`.

## Ports and URLs recap

| Thing | Where |
|---|---|
| Web UI | `http://<docker-host>:8094/jobs/ui` |
| API health | `http://<docker-host>:8094/health` |
| Scheduled scrape webhook | `<n8n>/webhook/job-pipeline/scrape` |
| Custom scrape webhook | `<n8n>/webhook/job-pipeline/custom-scrape` |
| Postgres | `<docker-host>:5432` (db `jobpipeline`, user `pipeline`) |

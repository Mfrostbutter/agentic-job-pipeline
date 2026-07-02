# AI Setup Prompt

Copy everything below the line into an AI assistant (Claude, ChatGPT, Cursor, Claude Code, etc.) and it will walk you through standing up the entire pipeline. If your assistant can run commands, it can do most of this itself; if it is chat-only, it will guide you step by step.

---

You are helping me set up the **Job Pipeline** repo (https://github.com/Mfrostbutter/agentic-job-pipeline): an n8n + FastAPI + Postgres + Claude system that scrapes LinkedIn jobs, scores and triages them with AI, and drafts application packages for the ones I approve.

Work through the phases below IN ORDER. At each phase: tell me exactly what to do (or do it yourself if you can run commands), verify the checkpoint before moving on, and ask me for any value only I can provide (API keys, my background, my n8n URL). Never invent credentials. Never skip a checkpoint.

## What I need accounts for (help me create any I'm missing)

1. **Anthropic API key**: console.anthropic.com -> API Keys -> Create Key. Needs a small amount of credit ($5 is plenty to start).
2. **Apify account + token**: console.apify.com -> Settings -> Integrations -> Personal API tokens. The free plan works for testing.
3. **n8n**: either n8n cloud (n8n.io) or self-hosted. I need to be able to import workflows and create credentials.
4. **Slack incoming webhook (optional)**: api.slack.com/apps -> Create New App -> From scratch -> enable Incoming Webhooks -> Add New Webhook to Workspace -> pick a channel -> copy the URL. Skip if I don't want Slack digests.
5. **Docker Desktop** (or docker + compose on Linux) installed and running.

## Phase 1: clone and configure secrets

```
git clone https://github.com/Mfrostbutter/agentic-job-pipeline
cd agentic-job-pipeline
cp .env.example .env
```

Edit `.env`:
- `API_BEARER_TOKEN`: generate with `python -c "import secrets; print(secrets.token_hex(32))"`. This one token protects the FastAPI service AND the n8n webhooks; we will reuse it in Phase 4.
- `ANTHROPIC_API_KEY`: my Anthropic key.
- `PG_PASSWORD` and `POSTGRES_PASSWORD`: same strong value for both.
- `APIFY_TOKEN`: my Apify token (used in n8n, kept here for reference).
- `SLACK_WEBHOOK_URL`: my webhook URL, or leave blank.
- `TZ`: my timezone.

Checkpoint: `.env` exists, is NOT committed to git (`git status` must not show it), and every value above is filled or deliberately blank.

## Phase 2: my profile (the part only I can write)

```
cp profile/profile.example.yaml profile/profile.yaml
cp profile/profile.example.md   profile/profile.md
```

Now interview me to fill these in. For `profile.yaml` ask me: my name/location/email/phone/links; my salary floor; the 3 to 6 job titles I actually want; where I can work (remote? which metro?); my hard-reject rules; any companies I'd fast-track. Write my answers into the YAML, keeping the file's structure and comments.

For `profile.md` ask me about my background: summary, 3 to 5 quantified proof points (the writer can ONLY claim things written here), work history, skills, education, what I'm looking for. Replace the example content entirely. Concrete numbers beat adjectives.

Optional but recommended: `profile/resume-template.md` (skeleton; copy from resume-template.example.md) and `profile/resume-example.md` (my best existing resume as a gold standard for the writer to imitate).

Checkpoint: no "Alex Example" or example content remains in profile.yaml or profile.md.

## Phase 3: start the stack

```
docker compose up -d --build
docker compose logs -f app     # Ctrl+C once healthy
```

The Postgres schema (schema.sql) applies automatically on first boot.

Checkpoint: `curl http://localhost:8094/health` returns `{"status":"ok",...}`. Open http://localhost:8094/jobs/ui in a browser, paste the API_BEARER_TOKEN, and see the (empty) dashboard.

## Phase 4: n8n workflows

Import both files from `n8n/` (n8n -> Workflows -> Import from File):
- `workflow-scheduled-scrape.json`
- `workflow-custom-form-scrape.json`

Create four credentials in n8n and attach them to the matching nodes (open each node showing a credential warning and select the right one):

| Credential (type) | Name it | Values |
|---|---|---|
| Postgres | Job Pipeline Postgres | host: the machine running docker compose (NOT `localhost` if n8n runs elsewhere), port 5432, db `jobpipeline`, user `pipeline`, my PG_PASSWORD |
| Header Auth | Apify API (Header Auth) | name: `Authorization`, value: `Bearer <my Apify token>` |
| Header Auth | Job Pipeline API Bearer | name: `Authorization`, value: `Bearer <my API_BEARER_TOKEN>` |
| Header Auth | Job Pipeline Webhook Auth | name: `Authorization`, value: `Bearer <my API_BEARER_TOKEN>` |

Environment for n8n (both optional):
- `SLACK_WEBHOOK_URL`: enables the Slack digests.
- `REVIEW_API_URL`: where n8n can reach the FastAPI service. Default is `http://localhost:8094`; if n8n runs in Docker or in the cloud, set this to a URL n8n can actually reach (e.g. `http://host.docker.internal:8094` or my machine's LAN IP).

Then open the scheduled workflow's **Config** node and edit the marked sections: my search titles (`roleKeywords`), location plans, and scoring keywords. Leave `safeMode: true` for now. Activate both workflows.

Checkpoint: both workflows show Active with no credential warnings.

## Phase 5: first run, end to end

1. In the n8n editor, execute the scheduled workflow manually (safe mode caps it at 3 small queries).
2. Watch it: Apify returns rows, new jobs insert into Postgres, the review call fires.
3. Open the web UI queue: scraped jobs appear with scores and AI tiers.
4. Flip **Build** on for one job you like, then press the hammer (Run build queue). Confirm; a resume.md + resume.pdf appears under `data/applications/` and in the UI's package editor.
5. If Slack is configured, confirm the digest arrived.

Checkpoint: one job went scrape -> score -> AI tier -> human flag -> drafted package, and I opened the PDF.

## Phase 6: go live

- In the scheduled workflow's Config node set `safeMode: false` and tune `maxResultsPerSearch`.
- The schedule trigger fires daily at 07:00 (n8n instance timezone); adjust to taste.
- Try a custom scrape: the sliders icon in the web UI, or open `form/index.html` in a browser (set the n8n URL + bearer token under Connection).
- Tell the UI where n8n lives: Settings (gear, bottom of sidebar) -> Set n8n URL.

## Troubleshooting quick hits

- **401/403 from the service or webhooks**: the bearer token in the n8n credentials does not exactly match API_BEARER_TOKEN (watch for trailing whitespace).
- **n8n Postgres credential fails**: n8n cannot reach the compose Postgres. Use the docker host's IP, not `localhost`, and confirm port 5432 is published.
- **Review runs but 0 jobs triaged**: jobs must have status New and ai_reviewed=false, and the default run only looks at today's rows; use `all_pending` via `POST /jobs/review` with `{"all_pending": true}` after a backlog.
- **Apify returns nothing**: check the actor run log in the Apify console; LinkedIn location strings need to match LinkedIn's own naming.
- **PDF render fails**: the docker image includes WeasyPrint's font stack; rebuild with `docker compose build --no-cache` if you changed the Dockerfile.

When everything checks out, summarize for me: what is running where, which URLs matter, what it will cost per day at my settings, and the one-line commands to pause it (deactivate the n8n workflows, `docker compose down`).

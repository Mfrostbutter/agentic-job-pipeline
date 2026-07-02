# Recruiter mode: the same engine, pointed the other way

The pipeline is a scrape -> score -> human review -> agent draft engine. Nothing in the code knows it is "for job hunting"; that intent lives entirely in three places you control:

1. **What you scrape** (workflow Config node / custom-scrape form)
2. **How you score and triage** (the `scoring` block + `profile.yaml` rubric)
3. **What the writer drafts** (`profile.md` + the templates)

Retarget those three and a job-search tool becomes a sourcing and warm-outreach tool for a recruiter, staffing agency, or fractional/consulting practice. A fresh job posting is the strongest buying signal there is: the company just told the world it has a funded need.

## What changes, knob by knob

### 1. Scrape: search for demand signals, not dream jobs

In the scheduled workflow's Config node (or per-run in the custom form):

```js
// A recruiter staffing data teams:
const roleKeywords = [
  'Data Engineer',
  'Analytics Engineer',
  'Head of Data',
];
// An agency selling automation services:
// const roleKeywords = ['Automation Engineer', 'RPA Developer', 'Integration Engineer'];
```

Use `run_label` on custom scrapes to keep client searches separate: `run_label: "client-acme-data-team"` tags every inserted row, and the label is stored on the job record.

### 2. Score: rank by how good a PROSPECT the posting is

The `scoring` block already has the right levers, they just mean different things now:

- `titleKeywords`: the roles your bench (or service) fills
- `bonusJDKeywords`: buying signals in the JD, e.g. `['urgent', 'immediate start', 'multiple positions', 'growing team']`, or tech that matches your candidates
- `homeAreaKeywords`: your territory
- `negativeTitlePatterns`: roles you do not staff
- `salaryFloor`: filters out engagements too small to bother with

### 3. Triage rubric: fit means "can I fill/serve this?"

In `profile/profile.yaml`, the rubric fields become your desk:

```yaml
candidate:
  name: Jordan the Recruiter        # the AI acts on Jordan's behalf
search:
  target_titles: [Data Engineer, Analytics Engineer]
  title_guidance: >
    "top" means a role my current bench can fill within two weeks, or a
    company likely to retain us for a search. "second" means plausible but
    needs a new sourcing effort. Agencies posting on behalf of clients are
    fine; dedupe them mentally against the end client.
  location_policy: >
    My candidates are US-remote or Chicago local.
  auto_reject_rules:
    - Internal-only postings that explicitly refuse agencies
    - Roles below $90K (fee too small)
  preferred_companies: []           # accounts you already have a relationship with
```

And `profile/profile.md` becomes your **desk digest**: who you place, recent placements with numbers ("placed 4 senior data engineers at Series B fintechs in the last two quarters"), your differentiators, your terms. The researcher/writer can only claim what is in this file, which keeps outreach honest.

### 4. Draft: outreach instead of resumes

The build step drafts whatever the profile grounds. Two practical options:

- **Light touch**: keep the resume writer as-is but treat `resume.md` as a capability one-pager; put your agency's track record in `profile.md` and a one-pager skeleton in `profile/resume-template.md`. The critic still enforces grounding and banned phrases.
- **Full outreach mode**: edit the writer prompt in `pipeline/build_packages.py` (`build_writer_system`) to output `outreach-email.md` instead: a 120-word first-touch email to the hiring manager referencing the specific posting (the JD's signal phrases are already extracted for you by the researcher step). Update `PACKAGE_FILE_WHITELIST` in `app/jobs_api.py` if you add new filenames you want editable in the UI.

### The human gate stays

Exactly as in job-search mode, nothing is drafted until you flip **Build** on a row. Review the queue, flag the postings worth a touch, batch-draft, then personalize and send yourself. The Reject-override feedback loop works unchanged: every time you rescue a posting the AI rejected, the next triage run learns your desk a little better.

## A concrete daily recruiter loop

1. 07:00 scheduled scrape: your territory + the titles you staff, past 24h.
2. Slack digest: "23 new postings, 4 top".
3. Queue: the AI already rejected the agencies-not-welcome and out-of-territory rows.
4. Flag the 4 top rows, run the build queue, get 4 grounded outreach drafts.
5. Personalize the first line of each, send, mark Applied, and use the stage funnel (Heard Back / Screen / Interview / Offer) as your prospect pipeline: Contacted / Replied / Meeting / Signed.

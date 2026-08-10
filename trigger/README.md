# `trigger/` — the external clock for the ingester

A Cloudflare Worker that calls GitHub's workflow-dispatch API on a cron, so the ingester does
not depend on GitHub's own `schedule:` trigger.

## Why this is not just a cron in the workflow file

`.github/workflows/ingest.yml` already asks for two runs an hour. Measured over the first 18
hours of deployment, from the public Actions API:

| Trigger | Runs | Behaviour |
|---|---|---|
| `schedule` (GitHub cron) | 11 | 15–22 min late, **31% of requested ticks**, holes of 102–167 min |
| `workflow_dispatch` (API) | 4 | **0 seconds delay, all four** |

Not once in 18 hours did two scheduled ticks land in the same hour, which is what independent
random dropping would produce. Moving the cron off the contended `:05`/`:35` slots to
`:13`/`:43` changed nothing and made the delay worse — that ruled out slot contention and left
throttling of the scheduled-event queue.

Two bulletins (`07:30` and `08:30` UTC on 2026-08-10) exist in the database only because
someone happened to run the ingester by hand during a 157-minute hole. CPCB keeps each hourly
bulletin on the feed for about an hour and publishes no archive, so a hole that wide loses
data permanently.

The GitHub cron is **left running on purpose**: it costs nothing, it is a backup if Cloudflare
or the token fails, and it keeps the control measurement going.

## Setup

Requires a free Cloudflare account. No credit card, no paid plan — Cron Triggers work on the
free tier.

### 1. Create the GitHub token

GitHub → Settings → Developer settings → **Fine-grained personal access tokens** → Generate new.

| Field | Value |
|---|---|
| Repository access | **Only select repositories** → `SUMEET1000/aqi-nowcast` |
| Repository permissions | **Actions: Read and write** — nothing else |
| Expiration | The shortest you will actually remember to rotate |

Do **not** grant `Contents: write`. That is what `repository_dispatch` would have needed, and
it would let anyone holding this token push a workflow that reads this repo's Actions secrets.
`Actions: write` can start a run and nothing else.

### 2. Prove the token works before deploying anything

```bash
curl -i -X POST \
  -H "Authorization: Bearer <YOUR_PAT>" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/SUMEET1000/aqi-nowcast/actions/workflows/ingest.yml/dispatches \
  -d '{"ref":"main"}'
```

Expect a 2xx (204 or 200). Then confirm a run actually appeared, within seconds:

```bash
curl -s "https://api.github.com/repos/SUMEET1000/aqi-nowcast/actions/workflows/ingest.yml/runs?per_page=3" \
  | grep -E '"(event|created_at|conclusion)"'
```

- **403** → the token is missing `Actions: Read and write`.
- **404** → the token is not scoped to this repository (GitHub returns 404, not 403, so it does
  not confirm that a resource exists).
- **422** → `ref` is wrong, or `ingest.yml` has no `workflow_dispatch:` trigger.

### 3. Deploy

```bash
cd trigger
npm install -g wrangler     # or: npx wrangler <command>
wrangler login
wrangler secret put GH_PAT  # paste the token — it is encrypted, never in the repo
wrangler deploy
```

**Before the first deploy, open Workers & Pages once in the Cloudflare dashboard.** The
account needs a `workers.dev` subdomain to exist, even though this Worker sets
`workers_dev = false` and is never published to one. Without it the schedules API fails with
error 10063 and the deploy reports `No targets deployed` — the code uploads, no cron is
registered, and nothing ever fires. Confirm the deploy prints a **Cron schedules** line
listing `5,35 * * * *`; an upload without that line is not a working deployment.

### 4. Watch one fire

```bash
wrangler tail
```

At the next `:05` or `:35` the invocation should complete with no error. Then check the Actions
API again: a run with `"event": "workflow_dispatch"` should appear within seconds of the
minute, not 20 minutes later.

## Failure modes worth knowing

- **The PAT expires.** Fine-grained tokens always do. When it lapses this Worker starts
  throwing and ingestion silently falls back to GitHub's throttled cron — the pipeline gets
  slower, not obviously broken. `scripts/gate1_check.py` reports the fraction of hours with at
  least one run, which is what catches this.
- **A green Cloudflare invocation is not proof of ingestion.** It only proves the dispatch was
  accepted. The proof is a `fetch_log` row whose `run_ts` matches the run.
- **Never commit `.dev.vars`.** Wrangler writes local secrets there in plaintext. It is
  gitignored, along with `.wrangler/`.

// Fires this repo's workflows by asking GitHub to run them, on a cron GitHub
// does not throttle.
//
// This file holds no secret. The GitHub token lives only as an encrypted Worker
// secret (`wrangler secret put GH_PAT`) and is never in the repo, never printed,
// and never returned in a response. See trigger/README.md.

const OWNER = "SUMEET1000";
const REPO = "aqi-nowcast";

// Which cron fires which workflow. Keyed by the exact schedule strings in
// wrangler.toml — Cloudflare hands the matched one back as event.cron, so the
// two files have to agree character for character.
//
// The daily send is here for the same reason ingestion is: GitHub's scheduled
// queue delivered 11 of ~36 requested ticks on this repo. A twice-hourly job
// survives that by running again; a once-a-day job does not — it is simply
// missing on most mornings, and Gate 2 is measured in whether people keep
// opening the message.
//
// The weekly monitor is here for a different reason from the other two. Its
// tick is the rarest in the system — 52 a year — so GitHub's ~31% delivery rate
// does not average out over it: a missed week is a week of the stubble season
// with no drift verdict, and the season is about six weeks long.
const WORKFLOWS = {
  "5,35 * * * *": "ingest.yml",
  "30 1 * * *": "send_alerts.yml", // 07:00 IST
  "0 2 * * 2": "monitor.yml", // Tuesday, after the scored week's data lands
};

// The workflow-dispatch endpoint, deliberately, rather than
// repository-dispatch. repository_dispatch needs a token with
// `Contents: write`, and a token that can write repository contents can push a
// workflow file — which then runs with this repo's Actions secrets, so a leak
// at the trigger host would expose DATABASE_URL and DATA_GOV_IN_API_KEY. GitHub
// masks registered secrets in logs, but masking does not survive an attacker
// who base64-encodes the value first.
//
// workflow_dispatch needs only `Actions: write`, which can start a run and
// nothing else. Same result, far smaller blast radius if the token ever leaks.
const endpoint = (workflow) =>
  `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${workflow}/dispatches`;

// Three attempts, ~1s then ~2s apart. This Worker is the only trigger that
// fires on time — GitHub's own cron is throttled to ~31% delivery and 15-22 min
// late (trigger/README.md) — so a single transient failure here is not "retry
// next tick", it is a bulletin that may never be captured: CPCB keeps each one
// on the feed for about an hour and publishes no archive. Three and not more
// because a cron invocation should not sit for a minute and the next tick is
// only 30 minutes away.
const ATTEMPTS = 3;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function attemptDispatch(env, workflow) {
  const res = await fetch(endpoint(workflow), {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_PAT}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      // Required. GitHub rejects API calls with no User-Agent, and the Workers
      // runtime does not set one for you.
      "User-Agent": "aqi-nowcast-trigger",
    },
    // `ref` is required. The workflow is read from this branch, and GitHub only
    // runs `schedule:`/dispatch against the default branch.
    body: JSON.stringify({ ref: "main" }),
  });

  // No silent fallbacks (build plan §0.5). GitHub's REST docs are inconsistent
  // about whether this endpoint answers 204 or 200, so accept any 2xx rather
  // than hardcoding one and failing on the other.
  if (res.ok) return;

  // A 401/403/404 is a fact about the credential, not a transient one, so
  // retrying just burns the invocation three times over:
  //   401 — the PAT expired. Fine-grained PATs always do.
  //   403 — the PAT is missing the `Actions: write` repository permission.
  //   404 — the PAT is not scoped to this repository at all. GitHub answers 404
  //         rather than 403 so it does not confirm a private resource exists.
  // Marked non-retryable so the error surfaces on the first attempt with the
  // real status still attached, instead of as three identical failures.
  const permanent = res.status === 401 || res.status === 403 || res.status === 404;
  const err = new Error(
    `dispatch of ${workflow} failed: HTTP ${res.status} ${await res.text()}`);
  err.permanent = permanent;
  throw err;
}

async function dispatch(env, workflow) {
  let last;
  for (let attempt = 1; attempt <= ATTEMPTS; attempt++) {
    try {
      return await attemptDispatch(env, workflow);
    } catch (e) {
      last = e;
      // A network-level rejection has no .permanent, so it retries — that is
      // the case this loop mainly exists for.
      if (e.permanent || attempt === ATTEMPTS) break;
      console.log(`dispatch attempt ${attempt}/${ATTEMPTS} failed: ${e.message}`);
      await sleep(1000 * attempt);
    }
  }

  // Still throws (§0.5) — a failed dispatch must fail the invocation.
  //
  // Known gap, left open on purpose: this surfaces only in Cloudflare's error
  // metrics and `wrangler tail`, and Cloudflare does not email on Worker
  // exceptions, so a dead PAT degrades to GitHub's throttled cron quietly.
  // Wiring an alert channel now needs either a Telegram token (Phase 2
  // scaffolding, which build plan §0.2 forbids ahead of its phase) or a PAT
  // widened beyond `Actions: write` to open issues, which would undo the
  // blast-radius decision above. It belongs to Phase 5 monitoring; until then
  // the backstop is gate1_check.py's hours-covered number.
  throw last;
}

export default {
  // Cron trigger. Cloudflare invokes this on the schedule in wrangler.toml.
  //
  // Awaited, not handed to ctx.waitUntil(). waitUntil would let this handler
  // return successfully while the dispatch rejected in the background, so the
  // invocation would be recorded as a success and the failure would show up
  // only as an unhandled rejection. Awaiting makes a failed dispatch fail the
  // invocation, which is the whole point of throwing above (§0.5).
  async scheduled(event, env, ctx) {
    const workflow = WORKFLOWS[event.cron];
    // Throwing rather than defaulting to ingest.yml (§0.5). A cron in
    // wrangler.toml with no entry here means the two files drifted apart, and
    // silently ingesting on the send's schedule would hide that for weeks.
    if (!workflow) throw new Error(`no workflow mapped for cron ${event.cron}`);
    await dispatch(env, workflow);
  },

  // Reachable only over a Cloudflare service binding from bot/ — this Worker has
  // workers_dev = false and no routes, so there is no URL to POST at from the
  // internet. That is the whole point: bot/ needs a send fired when someone
  // subscribes, and this is how it gets one without the GitHub PAT moving into
  // an internet-facing process.
  //
  // No inputs. send_alerts.py sends to every unpaused subscriber with no
  // sent_log row for today, so a run started by a new subscription is a no-op
  // for everyone who already has their message.
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("not found", { status: 404 });
    await dispatch(env, "send_alerts.yml");
    return new Response("ok");
  },
};

// Fires scripts/ingest.py by asking GitHub to run the `ingest` workflow.
//
// This file holds NO secret. The GitHub token lives only as an encrypted Worker
// secret (`wrangler secret put GH_PAT`) and is never in the repo, never printed,
// and never returned in a response. See trigger/README.md.

const OWNER = "SUMEET1000";
const REPO = "aqi-nowcast";
const WORKFLOW = "ingest.yml";

// Deliberately the workflow-dispatch endpoint, NOT repository-dispatch.
//
// repository_dispatch requires a token with `Contents: write`. A token that can
// write repository contents can push a workflow file, and a pushed workflow runs
// with this repo's Actions secrets — so a leak at the trigger host would expose
// DATABASE_URL and DATA_GOV_IN_API_KEY. GitHub masks registered secrets in logs,
// but masking does not survive an attacker who base64-encodes the value first.
//
// workflow_dispatch needs only `Actions: write`, which can start a run and
// nothing else. Same result, far smaller blast radius if the token ever leaks.
const ENDPOINT =
  `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;

async function dispatch(env) {
  const res = await fetch(ENDPOINT, {
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
  // than hardcoding one and failing on the other. Everything else throws, which
  // surfaces in the Workers error metrics and in `wrangler tail`.
  //
  // A 403 here almost certainly means the PAT is missing the `Actions: write`
  // repository permission; a 404 usually means the token is not scoped to this
  // repository at all (GitHub returns 404 rather than 403 to avoid confirming
  // that a private resource exists). A 401 means it expired — fine-grained PATs
  // do expire, and when that happens ingestion quietly falls back to GitHub's
  // throttled cron rather than stopping outright, which is why gate1_check.py
  // measures hours-covered instead of trusting that this ran.
  if (!res.ok) {
    throw new Error(
      `dispatch failed: HTTP ${res.status} ${await res.text()}`,
    );
  }
}

export default {
  // Cron trigger. Cloudflare invokes this on the schedule in wrangler.toml.
  //
  // AWAITED, not handed to ctx.waitUntil(). waitUntil would let this handler
  // return successfully while the dispatch rejected in the background — the
  // invocation would be recorded as a success and the failure would show up
  // only as an unhandled rejection. Awaiting means a failed dispatch fails the
  // invocation, which is the whole point of throwing above (§0.5).
  async scheduled(event, env, ctx) {
    await dispatch(env);
  },
};

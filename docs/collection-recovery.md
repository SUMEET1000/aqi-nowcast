# Collection recovery — 3–4 October 2026

**OpenAQ collection recovered; CPCB live-feed outage remains open.** This is an operational
recovery record, not evidence that the seasonal drift gate passed.

## What failed

[measured 2026-10-03: `python scripts/gate1_check.py`, Neon `fetch_log`,
Actions run 37132033410]
The last healthy CPCB ingest completed on 25 September at 14:05:50 UTC. Subsequent
requests failed; the latest stored bulletin remained 25 September at 13:30 UTC.
The first inspection counted 427 failures since that last success. Because OpenAQ
was a later step with the default success condition, it was skipped after every
CPCB failure, despite having newer archive data available.

Direct TLS checks from the development machine found data.gov.in's HTTPS port refused
connections; OpenAQ completed TLS. Google and Cloudflare DNS both returned the same
data.gov.in address. The Actions runner independently timed out on the CPCB request.
[measured 2026-10-03: socket/TLS probes, DNS-over-HTTPS lookups and Actions logs]
This establishes an endpoint availability problem; its deeper cause is unknown.

## Repair and recovered data

The OpenAQ step uses `!cancelled()` so it still executes after a failed CPCB step.
The CPCB step retains its nonzero exit and the job remains failed. Manual cancellation
does not start another archive pull. No source is silently substituted for another,
and no freshness or promotion threshold was raised.

Scheduled pulls now use `--resume`: replay seven days, or start at a station's last
stored hour when it is older. This preserves recovery after an interruption or a
publishing lag longer than the rolling window. Explicit `--since` still works.

The existing backfill script was run for one station, then all 30, beginning
24 September to overlap the last stored day. Its idempotent upsert and per-station
commits were reused.

[measured 2026-10-03: `python scripts/backfill_openaq.py --station 15 --since
2026-09-24`, then `python scripts/backfill_openaq.py --since 2026-09-24`,
followed by read-only Neon queries]

| Metric | Before recovery | After recovery |
|---|---:|---:|
| OpenAQ rows | 213,279 | 219,495 |
| Latest stored observation, UTC | 24 September 16:30 | 2 October 07:30 |
| Rows dated October, UTC | 0 | 834 |
| Stations with an October observation | 0 | 29 |

The initial pulls added 4,889 rows. Verification then found an aggregation defect,
described below; their 25 changes to existing values cannot be called archive
corrections. Reconciliation added another 1,327 rows, bringing the net recovery to
6,216. Against a preserved post-recovery snapshot, 1,432 existing values changed;
none was deleted. Those changes span 11 August 23:30 to 26 September 15:30 UTC.
All 30 stations were reconciled, including NISE Gwal Pahari, which had previously
returned timeouts. Its recovered recent history does not add it to the August scores.
Panipat's sensor archive still ended on 30 September; a successful request does not
make that station current. The other 29 stations ended on 2 October at 07:30 UTC,
about 32.6 hours behind at the final database snapshot.
[measured 2026-10-03: bounded reconciliation from 2026-08-10 through
2026-10-02T08:30:00 and before/after Neon row comparison]

## Hourly aggregation defect

OpenAQ recomputes an edge bucket from the requested start while retaining its full
hour label. A controlled request for Ambala sensor 14258997 returned these results:

| Request start, UTC | Returned period, UTC | PM2.5, µg/m³ | Coverage |
|---|---|---:|---:|
| 26 September 15:30 | 15:30–16:30 | 21.2 | 100% |
| 26 September 15:45 | 15:30–16:30 | 19.8 | 75% |

Both requests ended at 19:30. Interior hours were unchanged. This isolated the request
boundary from an upstream restatement. Midnight UTC requests also cut the stations'
hours, which start at :30 UTC. The old importer accepted those partial aggregates
and could overwrite a full hour with them.
[measured 2026-10-03: paired hourly API requests with only datetime_from changed]

The importer now accepts a period only when both ends fall inside the request, and
reports how many edge buckets it skips. Missing bounds or a non-hourly period fail
visibly. Genuine low sensor coverage inside a full requested hour is still accepted.
Nine tests cover resume behavior, both edges, paging and malformed periods; the
original importer fails five assertions in the new boundary fixtures.

The affected collection period was repulled with complete intervals. The preserved
snapshot supports an exact before/after comparison; it does not identify every changed
value as boundary corruption rather than an upstream restatement. The measured Ambala
hour is now 21.2 in Neon. A request over the full archive hit HTTP 408; bounded requests
covered the affected period instead. Historical benchmark tables remain dated results
from their original snapshots and have not been rescored after this reconciliation.

The identical bounded request was then repeated for all 30 stations and changed
**zero rows**, with exit 0. [measured 2026-10-03: `python scripts/backfill_openaq.py
--since 2026-08-10 --until 2026-10-02T08:30:00`, repeated after reconciliation]

## Verification defects repaired

The send-window check previously iterated only days containing a bulletin. During
the outage it kept reporting that 05:00 was fresh, because the missing tail never
entered the calculation. It now checks every calendar day through today and uses
the latest bulletin available at each send, even from a previous day.

Eight deterministic tests cover a healthy feed, interior and tail gaps, the previous
day's bulletin, future observations and sends, and the IST date boundary. All pass
with the repair. The original implementation fails five of those fixtures.
On the live outage, the repaired command exits nonzero and reports a 178-hour-old
bulletin at the configured 05:00 send. Its 3-hour threshold is unchanged.
[measured 2026-10-03: `python tests/test_send_window.py`, replay against the original
implementation and `python scripts/check_send_window.py`]

Gate 1 now filters liveness to ingester outcomes. A recent Telegram or monitor row
cannot make a stopped ingester appear active. Four tests execute the actual queries
against temporary PostgreSQL tables, always rolled back; the original code fails
two of those fixtures. [measured 2026-10-03: `python tests/test_pipeline_freshness.py`
and replay against the original implementation]

A message preview also exposed stale government advice next to a newer OpenAQ
concentration. The composer now dates old observations, describes old dust in the
past tense, and withholds a government bulletin's score and health note when it
exceeds the existing 12-hour staleness limit. Fresh OpenAQ data cannot refresh an
old government bulletin. Tests cover both languages and the exact boundary.
The existing forecast issue-age limit remains 3 hours.

## Monitoring and model status

[measured 2026-10-03: `python scripts/monitor.py --backfill` after reconciliation]
The latest complete week, starting 21 September, changed from mean 59.6 µg/m³,
ratio 1.50 before recovery, to mean 48.3 µg/m³, ratio 1.21 against a reconciled
39.9 baseline. All 1,573 station-week snapshots were rebuilt, and the local model
input cache was refreshed from the corrected archive.
The initially missing later days had biased the weekly distribution upward.
The recovered week is inside the existing 0.5–1.5 band. This is not a detected
seasonal drift incident. The current partial week is excluded as designed.

The archive freshness check passed at about 32.6 hours against its 36-hour limit, but
all 30 CPCB stations remained in the already-reported stale state. A zero exit
from this change-triggered monitor is not a full pipeline-health verdict.
Gate 1 still fails on the live-feed age and historical success rate.
At the 3 October recovery snapshot, the recorded-run rate was 82.5%, below the
unchanged 95% limit. Restoring endpoint
access will not erase past failed runs or instantly restore that historical rate.

The 1 October retrain kept the incumbent: F2 0.479 against challenger 0.439 and
persistence 0.281 on 15,431 held-out rows and 424 events ending 24 September.
[measured 2026-10-03: `model_runs` 6/7 joined to Actions
36808527471/36845884793]
No model was force-promoted or retrained during recovery. A dry-run sender still
refused the forecast for stale input; the recovered daily-pattern block was available
for the sampled Ambala station. No diagnostic message was sent to a subscriber.
These stored model scores predate the archive reconciliation; they are not a new
evaluation on the corrected data.

## Deployment proof and 4 October follow-up

The collector repairs were pushed to `main` in `7ab0a24` and `2055d75`.
[Scheduled run 37147100274](https://github.com/SUMEET1000/aqi-nowcast/actions/runs/37147100274)
checked out `2055d75`, passed all nine archive tests, failed the CPCB step, then
completed the OpenAQ step for all 30 stations with **zero rows changed**. Its overall
failure preserves the source outage. The artifact is the per-station log and final
upsert count, backed by the unchanged Neon snapshot, rather than the job's tab color.
[measured 2026-10-04 IST: run steps and logs, followed by read-only Neon queries]

All 13 test scripts passed on 3 October. The unchanged monitoring and promotion
thresholds were checked against the original constants. The monitor self-test passed
again on 4 October, and its actual stale-input path still exited 1. Both workflows
parse, and their independent archive and pre-send message checks are present.

The first manual verification overlapped local recovery and exhausted the shared
OpenAQ quota: run 37134895702 passed its tests and collected seven stations before
HTTP 429 ended it. Scheduled ingestion was briefly disabled while the local repair
and identical repeat finished, then re-enabled. The scheduled proof above ran without
that overlap. Keep bulk local pulls separate from scheduled pulls using the same key.
[measured 2026-10-03: run 37134895702 logs and API rate-limit headers; 2026-10-04
IST: workflow state is active and scheduled run 37147100274 completes its archive step]

At **01:09 IST on 4 October**, the table still contained 219,495 rows and the newest
OpenAQ hour was still 2 October 07:30 UTC: **36.2 hours old**. The monitor now correctly
exits 1 against its existing 36-hour limit, even though the collector is succeeding.
Its diagnostic now asks for source coverage and step logs to distinguish a late
publisher from a failed collector; it no longer points only to a missing API key.
The latest complete week's drift ratio remains 1.21. This does not make the stale
inputs current. CPCB's bulletin was 198.2 hours old and recorded-run success was
82.3% over 3,300 ingester runs. Gate 1 and the send-window check both still exit 1.
[measured 2026-10-04 IST: read-only Neon snapshot, `python scripts/monitor.py`,
`python scripts/gate1_check.py` and `python scripts/check_send_window.py`]

## Remaining gate

Restore CPCB endpoint access and verify advancing live bulletins before calling the
collection pipeline healthy. Missing CPCB snapshots cannot be recreated from the
OpenAQ concentration archive, because they are different quantities. Keep collecting
OpenAQ independently while the live endpoint is down.

The next seasonal evaluation must use the current 05:00 IST send and measured input
availability, rather than reuse August's seven-hour lag assumption. Phase 5's seasonal
post-mortem remains open until there is a measured real shift and before/after model
evidence. The recovered archive and this incident record do not supply that evidence.

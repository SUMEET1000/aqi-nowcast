# Distribution — Gate 0.3 (template)

The real file (`docs/distribution.md`) is **gitignored**: it holds real people's names
alongside a health-adjacent product, which does not belong in a public repo (§5 privacy,
§10 legal/ethical). This template is the committed structure.

## Seed profile

`asthma_child` — parents deciding about school and outdoor play.

Chosen on **channel density, not prevalence.** `copd_elderly` has ~4× the raw prevalence
(27% of Indians 60+ vs ~6.5% of school children) but the patient holds the phone and is the
lowest Telegram-adoption cohort, with no organised group to seed into. For `asthma_child`
the decider is a 30–45yo parent already inside a school WhatsApp group with ~40 other
parents near the same station. One forward reaches 40 qualified people.

## Seed testers

| # | Name | Location | Station | Channel (wider group, not 1:1 contact) |
|---|---|---|---|---|
| 1 | | | | |
| 2 | | | | |
| 3 | | | | |

**The channel column is the point.** Direct contact reaches users 1–3 and stops. The entry
worth recording is the *wider group* each person sits in — college batch group, society/RWA
group, local city group — because that is the Phase 2 seeding path to users 4–40.

## Testers are not the market

Deliberate split:

- **Test on Ambala + Kurukshetra** — where the three testers actually are.
- **Market to Gurugram / Faridabad** — where `asthma_child` bites. At the same probe, Gurugram
  Teri Gram was 169 and Sector-51 was 124 against Ambala's 49. The advisory fires meaningfully
  there outside stubble season; in Ambala it mostly would not.

Two seed stations rather than one is also a free correctness check: **if Ambala and
Kurukshetra ever report identical values, that is a pipeline bug, not weather.** This is
enforced in code — see the seed-station check in `scripts/gate1_check.py`, which compares the
two PM2.5 series once there are enough readings for a match to mean something.

## Honest caveat, to be carried into the README

Friends and family do not produce retention signal; they subscribe because they were asked.
Gate 2 is therefore counted as passing on **mechanics** (message delivered, feedback row
written), never as evidence of demand. §9's rule applies: if the real number stays small,
delete the usage line rather than dress it up. "60 subscribers, 55% active at week 8" shows
better judgement than "60 subscribers"; a truthful 8 survives every follow-up question.

## Known limitation for the README (§10, Data)

`Patti Mehar` is in Ambala City, several km from Ambala Cantt; the Kurukshetra station is
~5km from Pipli. **Station AQI is not doorstep AQI.** Inherent to any station-based product —
stated, not glossed.

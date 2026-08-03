# Jobbie — Plan

Job-alert bot, same shape as stockie: scheduled script → find new job listings matching Tanishq's profile → push to Telegram.

## What it does

Every N hours: query job sources → normalize → drop already-seen jobs → filter by title/experience → send new ones to Telegram with direct apply links → persist seen IDs.

## Profile (from resume.tex)

- **Role**: Backend / SDE-1/2 — Node.js, TypeScript, microservices, fintech
- **Experience**: ~1 yr full-time + internships → filter jobs asking ≤ 3 yrs
- **Location**: India (Gurgaon / Jaipur / Remote)
- **Title match**: backend, node, SDE, software engineer, full stack

## Architecture

```
jobbie/
├── main.py                    # everything: fetch → dedupe → filter → send (~200 lines)
├── data/seen.json             # job IDs already sent (committed back by CI)
├── .env                       # local secrets (gitignored)
├── requirements.txt           # requests only
├── .github/workflows/jobs.yml # cron + manual dispatch
└── README.md
```

Single file, no classes, no framework. `requests` is the only dependency.

## Job sources (pluggable — enabled if its env key exists)

| Source | Env key | Covers | Cost |
|--------|---------|--------|------|
| JSearch (RapidAPI) | `RAPIDAPI_KEY` | LinkedIn + Naukri + Indeed + Glassdoor via Google for Jobs | free 200 req/mo |
| Apify `apimaestro/linkedin-jobs-scraper-api` | `APIFY_TOKEN` | LinkedIn direct | ~$5 / 1k jobs |
| Apify `memo23/naukri-scraper` | `APIFY_TOKEN` | Naukri direct | ~$0.6 / 1k jobs |

Each fetcher returns the same normalized shape: `{id, title, company, location, url, source, posted}`.

## Dedupe

`data/seen.json` = dict of `job_id → first_seen_date`. New job = not in dict. CI commits the updated file back after each run. Pruned to last ~5000 entries.

## Telegram

Reuse stockie's bot token + chat ID (already in stockie/.env) — alerts arrive on the same bot. HTML messages, one job per block, split at 4000 chars (Telegram limit), 400 → retry as plain text (stockie's trick).

## Scheduling

GitHub Actions cron (stockie pattern, fallback-tolerant since job alerts have no deadline):

- **2 runs/day (9:00 + 18:00 IST)** × 3 rotating queries = ~180 req/mo — fits JSearch free 200
- All 5 role queries covered every ~2 days via rotation (`state.runs` counter)
- `workflow_dispatch` for manual runs, `dry_run` input to print instead of send.

## Decisions made (2026-08-04)

- Roles: backend, full-stack, data eng, platform, AI — all 5, rotated
- Location: anywhere India + remote (no filter)
- Source: JSearch free tier only (Apify skipped — user wants free)
- Referrals: free LinkedIn people-search links per job (Recruiters / Engineers at that company) + tap-to-copy referral message in `<pre>`
- Resume intake: send PDF to bot → next run parses it (pypdf + keyword buckets, **no LLM**) → profile updated, confirmation sent
- **Separate new bot** (not stockie's) — token via @BotFather, `python main.py chatid` helper

## Steps

1. ~~Scaffold repo~~ ✅
2. `main.py` — fetchers, dedupe, filter, Telegram send, tiny `selfcheck` mode
3. `.env` prefilled with stockie's Telegram creds; `.gitignore` it
4. `jobs.yml` — cron every 3h, commit `seen.json` back
5. Local test: `selfcheck` + real Telegram send
6. `git init`, push to GitHub via `gh`, set secrets
7. User signs up: RapidAPI (JSearch, free) and/or Apify ($5 free credit/mo) → add keys as secrets

## Needs from Tanishq

- [ ] RapidAPI key (free: rapidapi.com → subscribe to JSearch) and/or Apify token (console.apify.com → Settings → API tokens)
- [ ] Confirm search keywords/locations (defaults: "node.js backend developer", India)
- [ ] Separate bot? (default: reuse stockie's bot)

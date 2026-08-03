# Jobbie 💼

Job-alert Telegram bot. It finds new postings matching your resume and sends
each one with a direct apply link, LinkedIn people-search links (recruiters and
engineers at that company), and a tap-to-copy referral message. Runs on free
tiers only — no server, no paid API.

## How it works

```
GitHub Actions cron (9:00 & 18:00 IST) ─┐
Cloudflare Worker (/jobs, resume PDF) ──┴─ main.py
      ├─ 33 company career boards (Greenhouse/Lever/Ashby/SmartRecruiters) — keyless, unlimited
      ├─ aggregators: JSearch, Jooble, Adzuna, Remotive — free tiers, 3 rotating queries per run
      ├─ filter: seen jobs, senior/fresher titles, non-SWE roles, experience asks above your band
      ├─ Telegram: one message per job — Apply · Recruiters · Engineers · copyable referral text
      └─ commit state back to the repo
```

Company boards are the backbone: they are the same JSON feeds the career pages
themselves use, so they cost nothing and never run out. The aggregators add
breadth on top, and each one switches on only if its key is present — losing
any single source degrades coverage instead of breaking the bot.

## Commands

| Command | What it does |
|---|---|
| `/jobs` | Hunt now — results in ~1 minute |
| `/profile` | Current search queries, experience band, referral pitch |
| `/help` | Command list |
| send a PDF | Re-derives the search profile from that resume |

## Setup

1. **Bot** — message [@BotFather](https://t.me/BotFather) → `/newbot` → token into `.env`
2. **Chat ID** — send the bot any message, then `python main.py chatid`
3. **Keys** (all optional — company boards work without any):
   - JSearch: [rapidapi.com](https://rapidapi.com) → *JSearch* → Basic (free, 200/mo)
   - Jooble: [jooble.org/api/about](https://jooble.org/api/about) (free, 500 requests)
   - Adzuna: [developer.adzuna.com](https://developer.adzuna.com) (free, ~1000/mo)
4. Test locally:
   ```bash
   pip install -r requirements.txt
   python main.py selfcheck
   python main.py --dry-run   # prints jobs instead of sending
   ```
5. Add the `.env` values as repo **Actions secrets**.
6. Interactive commands (optional) — deploy the Worker and point Telegram at it:
   ```bash
   cd worker && npx wrangler deploy
   npx wrangler secret put TELEGRAM_BOT_TOKEN     # also CHAT_ID, WEBHOOK_SECRET, GITHUB_TOKEN
   ```

## Tuning

Everything lives in `data/profile.json`:

- `companies` — the watchlist. Add any company whose careers URL contains
  `greenhouse.io/<slug>`, `jobs.lever.co/<slug>`, `ashbyhq.com/<slug>`, or
  `jobs.smartrecruiters.com/<slug>`.
- `queries` — search terms for the aggregators, rotated across runs
- `max_months` — experience ceiling; titles asking for more are dropped
- `pitch` — the line that goes into every referral message

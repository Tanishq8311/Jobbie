# Jobbie 💼

Job-alert Telegram bot. Twice a day it finds new postings matching your resume
and sends each one with an apply link, LinkedIn people-search links (recruiters
and engineers at that company), and a tap-to-copy referral message. Free-tier
only: JSearch (RapidAPI) + GitHub Actions + Telegram. No LLM, no server.

Sibling of [stockie](https://github.com/Tanishq8311/stockie) — same
serverless pattern.

## How it works

```
GitHub Actions cron (9:00 & 18:00 IST)
  └─ main.py
      ├─ getUpdates: resume PDF sent to the bot? → re-derive search profile (pypdf + keyword buckets)
      ├─ JSearch: 3 rotating queries from data/profile.json (aggregates LinkedIn/Naukri/Indeed/Glassdoor)
      ├─ filter: drop seen IDs (data/state.json), senior/lead titles, >3y experience asks
      ├─ Telegram: one message per job — Apply · Recruiters · Engineers · <pre> referral text
      └─ commit state back to the repo
```

Query rotation keeps usage at ~180 requests/month — inside JSearch's free 200.

## Setup

1. **Bot** — message [@BotFather](https://t.me/BotFather) → `/newbot` → put the token in `.env`
2. **Chat ID** — send your new bot any message, then `python main.py chatid`
3. **JSearch key** — [rapidapi.com](https://rapidapi.com) → search *JSearch* → subscribe **Basic (free)** → key into `.env`
4. Test locally:
   ```bash
   pip install -r requirements.txt
   python main.py selfcheck
   python main.py --dry-run   # prints jobs instead of sending
   python main.py             # sends to Telegram
   ```
5. Push to GitHub and add the three `.env` values as repo **Actions secrets**
   (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `RAPIDAPI_KEY`).

## Updating your profile

Send the bot a new resume PDF — the next run picks it up, rewrites the search
queries, and confirms. The referral pitch line lives in `data/profile.json`
(`"pitch"`) — edit it there.

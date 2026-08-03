"""Jobbie — new-job alerts to Telegram, free-tier only.

Each run: pick up any resume PDF sent to the bot (updates the search profile),
query JSearch (RapidAPI, free 200 req/mo) for new postings, dedupe against
data/state.json, and send each new job to Telegram with apply link, LinkedIn
people-search links (recruiters/engineers at that company), and a tap-to-copy
referral message. No LLM anywhere — resume parsing is keyword matching.

Usage: python main.py [--dry-run] | python main.py selfcheck
"""

import html
import json
import logging
import re
import sys
import time
import urllib.parse
from datetime import date, datetime, timezone
from hashlib import sha1
from io import BytesIO
from pathlib import Path

import requests

ROOT = Path(__file__).parent
STATE_PATH = ROOT / "data" / "state.json"
PROFILE_PATH = ROOT / "data" / "profile.json"
TIMEOUT = 30
QUERIES_PER_RUN = 3   # ponytail: 3 req x 2 runs/day = 180/mo, fits JSearch free 200
MAX_SEND = 12         # per run; unsent jobs stay unseen and show up next run

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("jobbie")

# Skill keywords found in the resume decide which query buckets are searched.
SKILL_BUCKETS = {
    "backend": ["node", "express", "typescript", "microservice", "rest api", "golang", "java"],
    "fullstack": ["react", "next.js", "frontend", "tailwind"],
    "data": ["spark", "pyspark", "databricks", "data factory", "etl", "airflow"],
    "platform": ["kubernetes", "docker", "aws", "azure", "distributed", "cloudflare"],
    "ai": ["llm", "claude", "gemini", "openai", "prompt", "agentic", "machine learning"],
}
BUCKET_QUERIES = {
    "backend": "node.js backend developer",
    "fullstack": "full stack developer react node.js",
    "data": "data engineer spark",
    "platform": "platform engineer microservices",
    "ai": "ai engineer llm",
}

TITLE_EXCLUDE = re.compile(
    r"(?i)\b(senior|sr\.?|staff|principal|lead|architect|manager|head|director|vp|"
    r"intern(ship)?|\.net|php|wordpress|salesforce|sap|drupal)\b"
)


def load_env():
    p = ROOT / ".env"
    if p.exists():
        import os
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def env(key):
    import os
    return os.environ.get(key, "")


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(obj, indent=1))


# ---------------- Telegram ----------------

def tg(method, **payload):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/{method}",
            json=payload, timeout=TIMEOUT,
        )
        return r.json() if r.status_code == 200 else (log.error("tg %s %d: %s", method, r.status_code, r.text[:200]) or None)
    except requests.RequestException as e:
        log.error("tg %s failed: %s", method, e)
        return None


def send(text, dry=False):
    if dry:
        print("-" * 60 + "\n" + re.sub(r"<[^>]+>", "", html.unescape(text)))
        return True
    ok = tg("sendMessage", chat_id=env("TELEGRAM_CHAT_ID"), text=text,
            parse_mode="HTML", disable_web_page_preview=True)
    if ok is None:  # stockie's trick: malformed HTML -> resend plain
        ok = tg("sendMessage", chat_id=env("TELEGRAM_CHAT_ID"),
                text=re.sub(r"<[^>]+>", "", html.unescape(text)))
    return ok is not None


# ---------------- resume intake (no LLM: keyword matching) ----------------

def parse_resume(pdf_bytes, old_profile):
    from pypdf import PdfReader  # only needed when a resume is actually uploaded
    text = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf_bytes)).pages).lower()
    buckets = [b for b, kws in SKILL_BUCKETS.items() if any(k in text for k in kws)]
    skills = sorted({k for kws in SKILL_BUCKETS.values() for k in kws if k in text})
    profile = dict(old_profile or {})
    profile.update({
        "queries": [BUCKET_QUERIES[b] for b in buckets] or list(BUCKET_QUERIES.values()),
        "skills": skills,
        "updated": date.today().isoformat(),
    })
    profile.setdefault("pitch", "I'm a software engineer with experience in " + ", ".join(skills[:4]) + ".")
    profile.setdefault("max_months", 36)
    return profile


def check_updates(state, profile):
    """Poll getUpdates once: a PDF sent to the bot becomes the new profile."""
    resp = tg("getUpdates", offset=state.get("offset", 0) + 1, timeout=0)
    if not resp or not resp.get("ok"):
        return profile
    for u in resp["result"]:
        state["offset"] = max(state.get("offset", 0), u["update_id"])
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != env("TELEGRAM_CHAT_ID"):
            continue
        doc = msg.get("document") or {}
        if doc.get("file_name", "").lower().endswith(".pdf"):
            f = tg("getFile", file_id=doc["file_id"])
            if not f or not f.get("ok"):
                continue
            url = f"https://api.telegram.org/file/bot{env('TELEGRAM_BOT_TOKEN')}/{f['result']['file_path']}"
            try:
                profile = parse_resume(requests.get(url, timeout=TIMEOUT).content, profile)
                save_json(PROFILE_PATH, profile)
                send("✅ <b>Resume received.</b>\nSkills: " + html.escape(", ".join(profile["skills"]) or "none found")
                     + "\nSearching: " + html.escape("; ".join(profile["queries"]))
                     + "\n\n(Pitch line for referral messages is kept as-is — edit data/profile.json to change it.)")
            except Exception as e:  # a bad PDF must not kill the job run
                log.error("resume parse failed: %s", e)
                send("⚠️ Couldn't read that PDF — try re-exporting it.")
        elif msg.get("text", "").startswith("/start"):
            send("👋 I'm Jobbie. Send me your resume as a PDF and I'll tailor job alerts to it. "
                 "Alerts go out twice a day.")
    return profile


# ---------------- jobs (JSearch) ----------------

def fetch_jsearch(query):
    try:
        r = requests.get(
            "https://jsearch.p.rapidapi.com/search-v2",
            params={"query": f"{query} in India", "num_pages": 1,
                    "date_posted": "3days", "country": "in"},
            headers={"X-RapidAPI-Key": env("RAPIDAPI_KEY"),
                     "X-RapidAPI-Host": "jsearch.p.rapidapi.com"},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            log.error("jsearch %d: %s", r.status_code, r.text[:200])
            return []
        data = r.json().get("data") or {}
        items = data.get("jobs", []) if isinstance(data, dict) else data  # v2 nests under data.jobs
    except (requests.RequestException, ValueError) as e:
        log.error("jsearch failed: %s", e)
        return []
    jobs = []
    for it in items:
        title, company = it.get("job_title") or "", it.get("employer_name") or ""
        jobs.append({
            "id": it.get("job_id") or sha1(f"{title}|{company}".encode()).hexdigest()[:16],
            "title": title, "company": company,
            "location": ", ".join(filter(None, [it.get("job_city"), it.get("job_state")])) or "India",
            "url": it.get("job_apply_link") or "",
            "via": it.get("job_publisher") or "JSearch",
            "min_months": (it.get("job_required_experience") or {}).get("required_experience_in_months"),
        })
    return jobs


def job_ok(job, profile):
    if not job["url"] or TITLE_EXCLUDE.search(job["title"]):
        return False
    m = job["min_months"]
    return m is None or m <= profile.get("max_months", 36)


# ---------------- message ----------------

def li_people(company, kw):
    q = urllib.parse.quote(f'"{company}" {kw}')
    return f"https://www.linkedin.com/search/results/people/?keywords={q}"


def referral_text(job, profile):
    return (f"Hi [Name], I came across the {job['title']} opening at {job['company']} and it closely "
            f"matches my profile. {profile.get('pitch', '')} Would you be open to referring me, or pointing "
            f"me to the right person? Happy to share my resume. Posting: {job['url']} — thanks so much!")


def job_message(job, profile):
    e = html.escape
    return (
        f"💼 <b>{e(job['title'])}</b>\n"
        f"🏢 {e(job['company'])} · 📍 {e(job['location'])} · via {e(job['via'])}\n"
        f"🔗 <a href=\"{e(job['url'])}\">Apply</a>"
        f" · <a href=\"{e(li_people(job['company'], 'recruiter'))}\">Recruiters</a>"
        f" · <a href=\"{e(li_people(job['company'], 'software engineer'))}\">Engineers</a>\n\n"
        f"📋 Referral message (tap to copy):\n<pre>{e(referral_text(job, profile))}</pre>"
    )


# ---------------- main ----------------

def main(dry=False):
    load_env()
    state = load_json(STATE_PATH, {"offset": 0, "runs": 0, "seen": {}})
    profile = load_json(PROFILE_PATH, None)

    if not dry:
        profile = check_updates(state, profile)
    if not profile:
        send("👋 No profile yet — send me your resume as a PDF to get started.", dry)
        save_json(STATE_PATH, state)
        return

    pool = profile["queries"]
    base = state["runs"] * QUERIES_PER_RUN
    queries = [pool[(base + i) % len(pool)] for i in range(min(QUERIES_PER_RUN, len(pool)))]
    log.info("queries: %s", queries)

    fresh = []
    for q in queries:
        for job in fetch_jsearch(q):
            if job["id"] not in state["seen"] and job_ok(job, profile) and all(j["id"] != job["id"] for j in fresh):
                fresh.append(job)

    log.info("%d new jobs", len(fresh))
    today = date.today().isoformat()
    for job in fresh[:MAX_SEND]:
        if send(job_message(job, profile), dry):
            state["seen"][job["id"]] = today
        time.sleep(1)
    if len(fresh) > MAX_SEND:
        send(f"…and {len(fresh) - MAX_SEND} more new jobs — they'll come in the next run.", dry)

    if not dry:
        state["runs"] += 1
        if len(state["seen"]) > 5000:  # ponytail: naive prune, oldest-first if it ever matters
            state["seen"] = dict(sorted(state["seen"].items(), key=lambda kv: kv[1])[-5000:])
        save_json(STATE_PATH, state)


def selfcheck():
    p = {"pitch": "I build Node.js services.", "max_months": 36}
    ok = {"id": "x", "title": "Backend Engineer", "company": "Acme", "location": "Remote",
          "url": "https://a.co/1", "via": "LinkedIn", "min_months": 24}
    assert job_ok(ok, p)
    assert not job_ok({**ok, "title": "Senior Backend Engineer"}, p), "senior must be excluded"
    assert not job_ok({**ok, "min_months": 60}, p), "5y experience must be excluded"
    assert not job_ok({**ok, "url": ""}, p), "no apply link must be excluded"
    assert "Acme" in referral_text(ok, p) and "[Name]" in referral_text(ok, p)
    assert "<pre>" in job_message(ok, p) and "linkedin.com/search" in job_message(ok, p)
    assert '"Acme" recruiter' in urllib.parse.unquote(li_people("Acme", "recruiter"))
    print("selfcheck OK")


def print_chat_id():
    """Setup helper: message the bot once, then run `python main.py chatid`."""
    load_env()
    resp = tg("getUpdates")
    chats = {str(u["message"]["chat"]["id"]) for u in (resp or {}).get("result", []) if "message" in u}
    print("chat id(s): " + (", ".join(chats) or "none — send the bot any message first"))


if __name__ == "__main__":
    if "selfcheck" in sys.argv:
        selfcheck()
    elif "chatid" in sys.argv:
        print_chat_id()
    else:
        main(dry="--dry-run" in sys.argv)

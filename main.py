"""Jobbie — new-job alerts to Telegram, free-tier only.

Each run: pick up any resume PDF sent to the bot (updates the search profile),
query JSearch (RapidAPI, free 200 req/mo) for new postings, dedupe against
data/state.json, and send each new job to Telegram with apply link, LinkedIn
people-search links (recruiters/engineers at that company), and a tap-to-copy
referral message. Resume parsing is keyword matching — no external service.

Usage: python main.py [--dry-run] | python main.py selfcheck
"""

import csv
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
JOBS_CSV = ROOT / "data" / "jobs.csv"  # every job ever sent, for a combined view in Excel
PROFILE_PATH = ROOT / "data" / "profile.json"
TIMEOUT = 30
QUERIES_PER_RUN = 3   # ponytail: 3 req x 2 runs/day = 180/mo, fits JSearch free 200
MAX_SEND = 12         # per run; unsent jobs stay unseen and show up next run

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("jobbie")

# Skill keywords found in the resume decide which query buckets are searched.
SKILL_BUCKETS = {
    "backend": ["node", "express", "typescript", "microservice", "rest api", "golang"],
    "java": ["java", "spring"],
    "fullstack": ["react", "next.js", "frontend", "tailwind"],
    "data": ["spark", "pyspark", "databricks", "data factory", "etl", "airflow"],
    "platform": ["kubernetes", "docker", "aws", "azure", "distributed", "cloudflare"],
    "ai": ["llm", "claude", "gemini", "openai", "prompt", "agentic", "machine learning"],
}
BUCKET_QUERIES = {
    "backend": "node.js backend developer",
    "java": "java backend developer",
    "fullstack": "full stack developer react node.js",
    "data": "data engineer spark",
    "platform": "platform engineer microservices",
    "ai": "ai engineer llm",
}

TITLE_EXCLUDE = re.compile(
    r"(?i)\b(senior|sr\.?|staff|principal|lead|architect|manager|head|director|vp|"
    r"intern(ship)?|trainee|campus|apprentice|"
    r"\.net|php|wordpress|salesforce|sap|drupal|"
    # "engineer" titles that are not software engineering
    r"sales|solution|support|delivery|network|field|customer|success|account|"
    r"specialist|consultant|analyst|recruiter|marketing|design|hardware|mechanical)\b"
)
# Company career boards list every role incl. sales/HR — keep only tech ones.
TECH_TITLE = re.compile(
    r"(?i)\b(engineer|developer|sde|software|backend|full.?stack|data|platform|devops|sre)\b"
)
INDIA_LOC = re.compile(
    r"(?i)(india|bengaluru|bangalore|gurugram|gurgaon|hyderabad|mumbai|pune|noida|delhi|chennai|jaipur|remote)"
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
    if ok is None:  # Telegram 400s on malformed HTML -> resend as plain text
        ok = tg("sendMessage", chat_id=env("TELEGRAM_CHAT_ID"),
                text=re.sub(r"<[^>]+>", "", html.unescape(text)))
    return ok is not None


# ---------------- resume intake (keyword matching) ----------------

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
    profile.setdefault("max_months", 24)
    return profile


def intake_resume(profile):
    """The webhook worker dispatches this run with the PDF's file_id as input."""
    file_id = env("FILE_ID")
    if not file_id:
        return profile
    f = tg("getFile", file_id=file_id)
    if not f or not f.get("ok"):
        return profile
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
    return profile


# ---------------- job sources ----------------
# All free official APIs. Each fetcher returns [] instantly when its key is
# missing, so adding a source is just adding a key to .env / repo secrets.

def _req(method, url, **kw):
    try:
        r = requests.request(method, url, timeout=TIMEOUT, **kw)
        if r.status_code != 200:
            log.error("%s %d: %s", url.split("/")[2], r.status_code, r.text[:150])
            return None
        return r.json()
    except (requests.RequestException, ValueError) as e:
        log.error("%s failed: %s", url.split("/")[2], e)
        return None


def fetch_jsearch(query):
    if not env("RAPIDAPI_KEY"):
        return []
    resp = _req("GET", "https://jsearch.p.rapidapi.com/search-v2",
                params={"query": f"{query} in India", "num_pages": 1,
                        "date_posted": "3days", "country": "in"},
                headers={"X-RapidAPI-Key": env("RAPIDAPI_KEY"),
                         "X-RapidAPI-Host": "jsearch.p.rapidapi.com"})
    data = (resp or {}).get("data") or {}
    items = data.get("jobs", []) if isinstance(data, dict) else data  # v2 nests under data.jobs
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


def fetch_adzuna(query):
    if not env("ADZUNA_APP_ID"):
        return []
    resp = _req("GET", "https://api.adzuna.com/v1/api/jobs/in/search/1",
                params={"app_id": env("ADZUNA_APP_ID"), "app_key": env("ADZUNA_APP_KEY"),
                        "what": query, "max_days_old": 3, "results_per_page": 20,
                        "sort_by": "date"})
    return [{
        "id": str(it.get("id", "")), "title": it.get("title") or "",
        "company": (it.get("company") or {}).get("display_name") or "",
        "location": (it.get("location") or {}).get("display_name") or "India",
        "url": it.get("redirect_url") or "", "via": "Adzuna", "min_months": None,
    } for it in (resp or {}).get("results", [])]


def fetch_jooble(query):
    if not env("JOOBLE_KEY"):
        return []
    resp = _req("POST", f"https://jooble.org/api/{env('JOOBLE_KEY')}",
                json={"keywords": query, "location": "India"})
    return [{
        "id": str(it.get("id", "")), "title": it.get("title") or "",
        "company": it.get("company") or "",
        "location": it.get("location") or "India",
        "url": it.get("link") or "", "via": "Jooble", "min_months": None,
    } for it in (resp or {}).get("jobs", [])[:20]]


def fetch_remotive(query):
    # Keyless. Remote roles only — keep the ones open to India.
    resp = _req("GET", "https://remotive.com/api/remote-jobs",
                params={"search": query, "limit": 20})
    jobs = []
    for it in (resp or {}).get("jobs", []):
        loc = (it.get("candidate_required_location") or "").lower()
        if not any(x in loc for x in ("india", "worldwide", "anywhere")):
            continue
        jobs.append({
            "id": str(it.get("id", "")), "title": it.get("title") or "",
            "company": it.get("company_name") or "",
            "location": f"Remote ({it.get('candidate_required_location')})",
            "url": it.get("url") or "", "via": "Remotive", "min_months": None,
        })
    return jobs


def fetch_boards(profile):
    """Watched companies' own career pages via Greenhouse/Lever public APIs.

    Free and unlimited — these are the same JSON feeds the career pages use.
    Runs once per run (not per query): a board lists everything it has.
    """
    jobs = []
    for c in profile.get("companies", []):
        board = c["board"]
        if board == "greenhouse":
            resp = _req("GET", f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs")
            found = [{"title": it.get("title") or "",
                      "location": (it.get("location") or {}).get("name") or "",
                      "url": it.get("absolute_url") or "", "id": str(it.get("id", ""))}
                     for it in (resp or {}).get("jobs", [])]
        elif board == "lever":
            resp = _req("GET", f"https://api.lever.co/v0/postings/{c['slug']}?mode=json")
            found = [{"title": it.get("text") or "",
                      "location": (it.get("categories") or {}).get("location") or "",
                      "url": it.get("hostedUrl") or "", "id": str(it.get("id", ""))}
                     for it in (resp or [])]
        elif board == "ashby":
            resp = _req("GET", f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}")
            found = [{"title": it.get("title") or "", "location": it.get("location") or "",
                      "url": it.get("jobUrl") or "", "id": str(it.get("id", ""))}
                     for it in (resp or {}).get("jobs", [])]
        else:  # smartrecruiters
            resp = _req("GET", f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings",
                        params={"limit": 100})
            found = [{"title": it.get("name") or "",
                      "location": (it.get("location") or {}).get("city") or "",
                      "url": f"https://jobs.smartrecruiters.com/{c['slug']}/{it.get('id')}",
                      "id": str(it.get("id", ""))}
                     for it in (resp or {}).get("content", [])]
        for f in found:
            if TECH_TITLE.search(f["title"]) and INDIA_LOC.search(f["location"] or "india"):
                jobs.append({**f, "company": c["name"], "via": f"{c['name']} careers",
                             "min_months": None})
    return jobs


SOURCES = [fetch_jsearch, fetch_adzuna, fetch_jooble, fetch_remotive]


def dedupe_key(job):
    """Same job on two boards must send once: key on title+company, not source id."""
    return sha1(f"{job['title'].lower().strip()}|{job['company'].lower().strip()}".encode()).hexdigest()[:16]


# Boards put the ask in the title: "SRE (4 to 8 years)", "Dev 5+ YOE".
TITLE_YEARS = re.compile(r"(?i)(\d{1,2})\s*(?:\+|to|-|–)?\s*(?:\d{1,2})?\s*(?:\+)?\s*(?:years?|yrs?|yoe)")


def match_score(job, profile):
    """Skill mentions in the title + a bump for watched-company boards."""
    title = job["title"].lower()
    return (sum(1 for s in profile.get("skills", []) if s in title)
            + (1 if job["via"].endswith("careers") else 0))


def job_ok(job, profile):
    if not job["url"] or TITLE_EXCLUDE.search(job["title"]):
        return False
    max_yrs = profile.get("max_months", 24) / 12
    m = TITLE_YEARS.search(job["title"])
    if m and int(m.group(1)) > max_yrs:
        return False
    months = job["min_months"]
    return months is None or months <= profile.get("max_months", 24)


def log_to_csv(job, today):
    new = not JOBS_CSV.exists()
    with JOBS_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "title", "company", "location", "score", "via", "url",
                        "recruiters", "engineers"])
        w.writerow([today, job["title"], job["company"], job["location"],
                    job.get("score", 0), job["via"], job["url"],
                    li_people(job["company"], "recruiter"),
                    li_people(job["company"], "software engineer")])


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
        profile = intake_resume(profile)
    if not profile:
        send("👋 No profile yet — send me your resume as a PDF to get started.", dry)
        return

    pool = profile["queries"]
    base = state["runs"] * QUERIES_PER_RUN
    queries = [pool[(base + i) % len(pool)] for i in range(min(QUERIES_PER_RUN, len(pool)))]
    log.info("queries: %s", queries)

    found = fetch_boards(profile)
    for q in queries:
        for src in SOURCES:
            found.extend(src(q))

    fresh, batch = [], set()
    for job in found:
        key = dedupe_key(job)
        # job["id"] check keeps pre-multi-source seen entries honoured
        if key in batch or key in state["seen"] or job["id"] in state["seen"]:
            continue
        if job_ok(job, profile):
            batch.add(key)
            job["score"] = match_score(job, profile)
            fresh.append(job)

    fresh.sort(key=lambda j: j["score"], reverse=True)  # best matches send first
    log.info("%d new jobs", len(fresh))
    today = date.today().isoformat()
    for job in fresh[:MAX_SEND]:
        msg = ("⭐ " if job["score"] >= 2 else "") + job_message(job, profile)
        if send(msg, dry):
            state["seen"][dedupe_key(job)] = today
            if not dry:
                log_to_csv(job, today)
        time.sleep(1)
    if len(fresh) > MAX_SEND:
        send(f"…and {len(fresh) - MAX_SEND} more new jobs — they'll come in the next run.", dry)

    if not dry:
        state["runs"] += 1
        if len(state["seen"]) > 5000:  # ponytail: naive prune, oldest-first if it ever matters
            state["seen"] = dict(sorted(state["seen"].items(), key=lambda kv: kv[1])[-5000:])
        save_json(STATE_PATH, state)


def selfcheck():
    p = {"pitch": "I build Node.js services.", "max_months": 24}
    ok = {"id": "x", "title": "Backend Engineer", "company": "Acme", "location": "Remote",
          "url": "https://a.co/1", "via": "LinkedIn", "min_months": 24}
    assert job_ok(ok, p)
    assert not job_ok({**ok, "title": "Senior Backend Engineer"}, p), "senior must be excluded"
    assert not job_ok({**ok, "min_months": 36}, p), "3y experience must be excluded"
    assert not job_ok({**ok, "title": "SRE (4 to 8 years)"}, p), "years-in-title must be read"
    assert not job_ok({**ok, "title": "Backend Dev 3+ YOE"}, p)
    assert job_ok({**ok, "title": "Backend Engineer (2-4 years)"}, p), "min 2y is in range"
    assert job_ok({**ok, "title": "Backend Engineer - Fresher"}, p), "freshers are fine"
    assert not job_ok({**ok, "title": "Sales Engineer"}, p), "non-SWE engineer excluded"
    assert not job_ok({**ok, "url": ""}, p), "no apply link must be excluded"
    assert "Acme" in referral_text(ok, p) and "[Name]" in referral_text(ok, p)
    assert "<pre>" in job_message(ok, p) and "linkedin.com/search" in job_message(ok, p)
    assert '"Acme" recruiter' in urllib.parse.unquote(li_people("Acme", "recruiter"))
    assert dedupe_key(ok) == dedupe_key({**ok, "id": "different", "via": "Adzuna"}), \
        "same title+company from two boards must dedupe"
    assert dedupe_key(ok) != dedupe_key({**ok, "company": "Other"})
    sp = {**p, "skills": ["node.js", "typescript"]}
    assert match_score({**ok, "title": "Node.js TypeScript Engineer"}, sp) == 2
    assert match_score({**ok, "via": "Stripe careers"}, sp) == 1, "watchlist boards get a bump"
    assert match_score(ok, sp) == 0
    import tempfile
    globals()["JOBS_CSV"] = Path(tempfile.mkdtemp()) / "jobs.csv"
    log_to_csv({**ok, "score": 2}, "2026-08-05")
    log_to_csv(ok, "2026-08-05")
    rows = JOBS_CSV.read_text().splitlines()
    assert len(rows) == 3 and rows[0].startswith("date,") and "Acme" in rows[1], "csv log broken"
    assert rows[1].count("linkedin.com/search") == 2, "recruiter+engineer links in csv"
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

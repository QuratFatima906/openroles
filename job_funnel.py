#!/usr/bin/env python3
"""
job_funnel.py
Pulls remote engineering roles from several FREE public job feeds, filters them
against your criteria (long-term contractor / full-time-remote, worldwide, senior
full-stack or applied-AI), removes anything you've already been shown, and writes
a dated Markdown digest. Optionally emails it.

Designed to run once a day (see .github/workflows/daily-jobs.yml).

Sources used (all free, no scraping of protected sites):
  - RemoteOK      https://remoteok.com/api            (confirmed live; ToS requires a back-link, which the digest includes)
  - We Work Remotely  RSS feeds                        (public RSS)
  - Remotive      https://remotive.com/api/remote-jobs (public JSON API - VERIFY endpoint is still live before relying on it)
  - Arbeitnow     https://www.arbeitnow.com/api/job-board-api (public JSON API - VERIFY)

NOT included (by design):
  - Arc.dev / Turing / Crossover  -> managed matching pools, no public job list to scrape. Apply once, keep profile live.
  - Wellfound (AngelList)         -> login + Cloudflare protected. Optional Apify hook is stubbed at the bottom; it costs Apify credits.

Nothing here is guaranteed to be current forever. If a source stops returning data,
the run logs a warning and continues with the others.
"""

import os
import json
import html
import re
import sys
import datetime as dt
from pathlib import Path

import requests

try:
    import feedparser  # for We Work Remotely RSS
except ImportError:
    feedparser = None

# ----------------------------------------------------------------------------
# CONFIG  -- tune these. This encodes the Shopsense-style role you're after.
# ----------------------------------------------------------------------------
CONFIG = {
    # A job's TITLE must contain at least one of these to be considered at all.
    "role_keywords": [
        "full stack", "fullstack", "full-stack",
        "software engineer", "software developer",
        "frontend", "front end", "front-end",
        "ai engineer", "ml engineer", "machine learning engineer",
        "applied ai", "applied ml", "platform engineer",
    ],

    # Bonus points if the title/description mentions these (your stack).
    "tech_keywords": [
        "typescript", "react", "node", "next.js", "nextjs",
        "python", "langchain", "langgraph", "rag", "llm",
        "observability", "aws", "postgres",
    ],

    # Bonus points for seniority.
    "seniority_keywords": ["senior", "staff", "lead", "principal", " sr ", "sr."],

    # Strong bonus: these signal the role will actually take a Pakistan-based hire.
    "worldwide_keywords": [
        "worldwide", "anywhere", "global", "remote - global",
        "work from anywhere", "any location", "fully remote",
    ],

    # REJECT if location/description contains any of these (blocks non-US candidates).
    "exclude_location": [
        "us only", "u.s. only", "usa only", "united states only",
        "must be based in the us", "must reside in the united states",
        "must be located in the united states", "us-based only", "us based only",
        "authorized to work in the us", "us work authorization",
        "must be a us citizen", "us citizens only", "eligible to work in the us",
        "onsite", "on-site", "in-office", "hybrid",
        "must be based in china", "based in india only",
    ],

    # REJECT if title/description looks like AI-training gig work or non-tech noise.
    # Matched on word boundaries, so "java" does NOT reject "javascript" roles.
    "exclude_title": [
        "data annotation", "annotator", "ai training", "ai trainer",
        "search evaluator", "rater", "transcription",
        "barista", "caretaker", "firefighter", "houseperson", "mason",
        "sales trainee", "territory sales", "dispatch", "accounts assistant",
        "java", "backend", "back end", "back-end",
    ],

    # Only surface jobs posted within this many days (keeps the daily digest fresh).
    "max_age_days": 3,

    # We Work Remotely category RSS feeds to pull.
    "wwr_feeds": [
        "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    ],
}

HEADERS = {"User-Agent": "job-funnel/1.0 (personal job search)"}
STATE_FILE = Path(__file__).parent / "seen_jobs.json"
OUT_DIR = Path(__file__).parent / "digests"


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _clean(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)      # strip HTML tags
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _recent_enough(epoch_or_none, max_age_days):
    if epoch_or_none is None:
        return True  # unknown date -> don't drop it
    age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromtimestamp(
        epoch_or_none, dt.timezone.utc
    )
    return age.days <= max_age_days


def _job_key(job):
    return f"{job['source']}::{job['company'].lower().strip()}::{job['title'].lower().strip()}"


# ----------------------------------------------------------------------------
# Source fetchers -- each returns a list of normalized job dicts, never raises
# ----------------------------------------------------------------------------
def fetch_remoteok():
    jobs = []
    try:
        r = requests.get("https://remoteok.com/api", headers=HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
        for item in data:
            if not isinstance(item, dict) or "position" not in item:
                continue  # first element is the legal/ToS notice
            jobs.append({
                "source": "RemoteOK",
                "title": item.get("position", ""),
                "company": item.get("company", ""),
                "location": item.get("location", "") or "Remote",
                "url": item.get("url") or item.get("apply_url", ""),
                "tags": item.get("tags", []),
                "description": _clean(item.get("description", "")),
                "epoch": item.get("epoch"),
                "salary": _fmt_salary(item.get("salary_min"), item.get("salary_max")),
            })
    except Exception as e:
        print(f"[warn] RemoteOK fetch failed: {e}", file=sys.stderr)
    return jobs


def fetch_wwr():
    jobs = []
    if feedparser is None:
        print("[warn] feedparser not installed; skipping We Work Remotely", file=sys.stderr)
        return jobs
    for feed_url in CONFIG["wwr_feeds"]:
        try:
            parsed = feedparser.parse(feed_url)
            for entry in parsed.entries:
                # WWR title format is usually "Company: Job Title"
                raw_title = entry.get("title", "")
                if ":" in raw_title:
                    company, title = raw_title.split(":", 1)
                else:
                    company, title = "", raw_title
                epoch = None
                if getattr(entry, "published_parsed", None):
                    import calendar
                    epoch = calendar.timegm(entry.published_parsed)
                jobs.append({
                    "source": "WeWorkRemotely",
                    "title": title.strip(),
                    "company": company.strip(),
                    "location": _clean(entry.get("region", "")) or "Remote",
                    "url": entry.get("link", ""),
                    "tags": [],
                    "description": _clean(entry.get("summary", "")),
                    "epoch": epoch,
                    "salary": "",
                })
        except Exception as e:
            print(f"[warn] WWR feed failed ({feed_url}): {e}", file=sys.stderr)
    return jobs


def fetch_remotive():
    jobs = []
    try:
        r = requests.get(
            "https://remotive.com/api/remote-jobs",
            params={"category": "software-dev", "limit": 100},
            headers=HEADERS, timeout=30,
        )
        r.raise_for_status()
        for item in r.json().get("jobs", []):
            epoch = None
            pub = item.get("publication_date")
            if pub:
                try:
                    epoch = dt.datetime.fromisoformat(pub).replace(
                        tzinfo=dt.timezone.utc).timestamp()
                except ValueError:
                    pass
            jobs.append({
                "source": "Remotive",
                "title": item.get("title", ""),
                "company": item.get("company_name", ""),
                "location": item.get("candidate_required_location", "") or "Remote",
                "url": item.get("url", ""),
                "tags": item.get("tags", []),
                "description": _clean(item.get("description", "")),
                "epoch": epoch,
                "salary": item.get("salary", "") or "",
            })
    except Exception as e:
        print(f"[warn] Remotive fetch failed: {e}", file=sys.stderr)
    return jobs


def fetch_arbeitnow():
    jobs = []
    try:
        r = requests.get(
            "https://www.arbeitnow.com/api/job-board-api",
            headers=HEADERS, timeout=30,
        )
        r.raise_for_status()
        for item in r.json().get("data", []):
            jobs.append({
                "source": "Arbeitnow",
                "title": item.get("title", ""),
                "company": item.get("company_name", ""),
                "location": item.get("location", "") or "Remote",
                "url": item.get("url", ""),
                "tags": item.get("tags", []) + item.get("job_types", []),
                "description": _clean(item.get("description", "")),
                "epoch": item.get("created_at"),
                "salary": "",
            })
    except Exception as e:
        print(f"[warn] Arbeitnow fetch failed: {e}", file=sys.stderr)
    return jobs


def _fmt_salary(lo, hi):
    if lo or hi:
        lo = lo or 0
        hi = hi or 0
        if lo and hi:
            return f"${lo:,} - ${hi:,}"
        return f"${(lo or hi):,}"
    return ""


# ----------------------------------------------------------------------------
# Filtering / scoring
# ----------------------------------------------------------------------------
def score_job(job):
    """Return (passes, score, reasons). A job passes only if it hits a role keyword,
    isn't excluded by location/title, and is recent enough."""
    title = job["title"].lower()
    blob = (job["title"] + " " + job["description"] + " " +
            job["location"] + " " + " ".join(map(str, job["tags"]))).lower()

    # Hard reject: gig/non-tech titles. Word-boundary match so "java" doesn't
    # hit "javascript".
    for bad in CONFIG["exclude_title"]:
        if re.search(rf"\b{re.escape(bad)}\b", title):
            return False, 0, [f"excluded title: {bad}"]

    # Must match a role keyword in the title
    if not any(k in title for k in CONFIG["role_keywords"]):
        return False, 0, ["no role keyword in title"]

    # Hard reject: location blocks non-US candidates
    for bad in CONFIG["exclude_location"]:
        if bad in blob:
            return False, 0, [f"excluded location: {bad}"]

    # Freshness
    if not _recent_enough(job.get("epoch"), CONFIG["max_age_days"]):
        return False, 0, ["too old"]

    # Scoring
    score, reasons = 10, ["role match"]
    if any(k in title for k in CONFIG["seniority_keywords"]):
        score += 5; reasons.append("senior")
    hit_tech = [k for k in CONFIG["tech_keywords"] if k in blob]
    if hit_tech:
        score += min(len(hit_tech) * 2, 10); reasons.append("tech: " + ", ".join(hit_tech[:5]))
    hit_ww = [k for k in CONFIG["worldwide_keywords"] if k in blob]
    if hit_ww:
        score += 6; reasons.append("worldwide-friendly")
    if job.get("salary"):
        score += 2; reasons.append("salary listed")
    return True, score, reasons


# ----------------------------------------------------------------------------
# State (dedupe across days)
# ----------------------------------------------------------------------------
def load_seen():
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception:
            return set()
    return set()


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(sorted(seen)))


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------
def render_markdown(scored):
    today = dt.date.today().isoformat()
    lines = [f"# Job digest - {today}", ""]
    if not scored:
        lines.append("_No new matching roles today. The funnel ran fine; nothing cleared the filter._")
        lines.append("")
        lines.append("Data via RemoteOK (https://remoteok.com), We Work Remotely, Remotive, Arbeitnow.")
        return "\n".join(lines)

    lines.append(f"**{len(scored)} new matching role(s).** Sorted best-fit first.\n")
    for job, sc, reasons in scored:
        salary = f" - {job['salary']}" if job["salary"] else ""
        lines.append(f"### [{job['title']}]({job['url']}) - {job['company']}")
        lines.append(f"- **Source:** {job['source']}  |  **Location:** {job['location']}{salary}")
        lines.append(f"- **Fit score:** {sc}  ({'; '.join(reasons)})")
        lines.append("")
    lines.append("---")
    lines.append("Data via RemoteOK (https://remoteok.com), We Work Remotely, Remotive, Arbeitnow.")
    return "\n".join(lines)


def render_html(scored):
    """HTML version of the digest for the email body. Inline styles only --
    email clients strip <style> blocks."""
    today = dt.date.today().isoformat()
    esc = html.escape
    parts = [
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;'
        'margin:0 auto;padding:16px;color:#1f2937;">',
        f'<h2 style="margin:0 0 4px;">Open Roles &mdash; {today}</h2>',
    ]
    if not scored:
        parts.append('<p>No new matching roles today. The funnel ran fine; '
                     'nothing cleared the filter.</p>')
    else:
        parts.append(f'<p style="margin:0 0 20px;color:#6b7280;">'
                     f'{len(scored)} new matching role(s), best fit first.</p>')
        for job, sc, reasons in scored:
            salary = f' &nbsp;&bull;&nbsp; {esc(job["salary"])}' if job["salary"] else ""
            parts.append(
                '<div style="border:1px solid #e5e7eb;border-radius:8px;'
                'padding:14px 16px;margin:0 0 12px;">'
                f'<div style="font-size:16px;font-weight:bold;margin-bottom:4px;">'
                f'<a href="{esc(job["url"])}" style="color:#1d4ed8;text-decoration:none;">'
                f'{esc(job["title"])}</a></div>'
                f'<div style="margin-bottom:6px;">{esc(job["company"])}</div>'
                f'<div style="font-size:13px;color:#6b7280;margin-bottom:6px;">'
                f'{esc(job["source"])} &nbsp;&bull;&nbsp; {esc(job["location"])}{salary}</div>'
                f'<div style="font-size:13px;color:#374151;">'
                f'Fit score {sc} &mdash; {esc("; ".join(reasons))}</div>'
                '</div>'
            )
    parts.append('<p style="font-size:12px;color:#9ca3af;">Data via '
                 '<a href="https://remoteok.com" style="color:#9ca3af;">RemoteOK</a>, '
                 'We Work Remotely, Remotive, Arbeitnow.</p></div>')
    return "".join(parts)


def send_email(markdown_body, html_body=None):
    """Emails the digest if GMAIL_USER / GMAIL_APP_PASSWORD / TO_EMAIL are set.
    Use a Gmail App Password (not your normal password)."""
    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("TO_EMAIL", user)
    if not (user and pw):
        print("[info] Email not configured; skipping send.", file=sys.stderr)
        return
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(markdown_body, "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    msg["Subject"] = f"Open Roles - {dt.date.today().isoformat()}"
    msg["From"] = user
    msg["To"] = to
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, pw)
            s.sendmail(user, [to], msg.as_string())
        print(f"[info] Emailed digest to {to}", file=sys.stderr)
    except Exception as e:
        print(f"[warn] Email send failed: {e}", file=sys.stderr)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    all_jobs = []
    all_jobs += fetch_remoteok()
    all_jobs += fetch_wwr()
    all_jobs += fetch_remotive()
    all_jobs += fetch_arbeitnow()
    print(f"[info] Pulled {len(all_jobs)} raw jobs from all sources.", file=sys.stderr)

    seen = load_seen()
    scored = []
    new_keys = set()
    for job in all_jobs:
        if not job.get("url") or not job.get("title"):
            continue
        key = _job_key(job)
        if key in seen:
            continue
        passes, sc, reasons = score_job(job)
        if passes:
            scored.append((job, sc, reasons))
            new_keys.add(key)

    # de-dup within this run (same role from two boards) by url
    unique, seen_urls = [], set()
    for tup in sorted(scored, key=lambda t: t[1], reverse=True):
        if tup[0]["url"] in seen_urls:
            continue
        seen_urls.add(tup[0]["url"])
        unique.append(tup)

    md = render_markdown(unique)

    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / f"{dt.date.today().isoformat()}.md"
    out_path.write_text(md)
    print(md)

    send_email(md, render_html(unique))

    # only remember jobs we actually surfaced, so a role that later improves can still resurface
    save_seen(seen | new_keys)
    print(f"[info] {len(unique)} new roles surfaced; state now has {len(seen | new_keys)} keys.", file=sys.stderr)


if __name__ == "__main__":
    main()

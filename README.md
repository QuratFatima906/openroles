# Open Roles

A small system that pulls remote engineering roles from free public job feeds, filters them to your criteria (long-term contractor / full-time remote, worldwide-friendly, senior full-stack or applied-AI), removes roles you've already seen, and emails you a daily digest. Runs itself once a day via GitHub Actions, no server needed.

## What it does

Every run it:
1. Pulls jobs from RemoteOK, We Work Remotely, Remotive, and Arbeitnow (all free).
2. Keeps only titles matching your role keywords, drops US-only / onsite / gig-training roles.
3. Scores each by seniority, your stack, and "worldwide" signals, sorts best-fit first.
4. Skips anything from a previous day (`seen_jobs.json`).
5. Writes `digests/YYYY-MM-DD.md` and, if configured, emails it.

## What it deliberately does NOT do

- **Arc.dev, Turing, Crossover** are managed matching pools with no public job list. There's nothing to scrape. Apply once, keep your profile active, let their matchers work.
- **Wellfound (AngelList)** is login + Cloudflare protected. A free scraper isn't reliable. If you want it, use an Apify actor (costs Apify credits) and feed results in, see the stub note in `job_funnel.py`.

## Run it locally

```bash
pip install -r requirements.txt
python job_funnel.py
```

The digest prints to your terminal and is saved in `digests/`.

## Make it run daily (free, no server)

1. Create a new **private** GitHub repo and push these files.
2. (Optional, for email) Create a Gmail **App Password**: Google Account -> Security -> 2-Step Verification -> App passwords. Then in the repo: Settings -> Secrets and variables -> Actions -> add:
   - `GMAIL_USER` = your gmail address
   - `GMAIL_APP_PASSWORD` = the 16-char app password
   - `TO_EMAIL` = where to send the digest (can be the same address)
3. That's it. The workflow in `.github/workflows/daily-jobs.yml` runs at 06:00 UTC (11 AM PKT) daily. You can also trigger it manually from the repo's **Actions** tab.

Without the email secrets it still runs and just commits the daily digest file to the repo, which you can read there.

## Tuning

All the criteria live in the `CONFIG` dict at the top of `job_funnel.py`:
- `role_keywords` - a title must match one of these
- `exclude_location` - kills US-only / onsite roles
- `exclude_title` - kills gig/annotation/non-tech noise
- `tech_keywords`, `seniority_keywords`, `worldwide_keywords` - scoring boosts
- `max_age_days` - how fresh a posting must be

## Verify before you trust

The RemoteOK and We Work Remotely feeds are well-established. The Remotive and Arbeitnow endpoints were included from public documentation but confirm they still return data on your first run (the script logs a warning and continues if one is down). Each surfaced job links back to the source posting; **always open the real posting and confirm it accepts a Pakistan-based contractor before investing time**, since no filter catches every US-authorization clause.

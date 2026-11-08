"""Daily health check for the tournament bot.

Checks the three things that cost us points in Fall 2026:
  1. Coverage: every open Fall / MiniBench question older than the grace period
     should already carry our forecast.
  2. Budget: the Metaculus-funded OpenRouter key must have credit left.
  3. Liveness: the forecasting workflow must have run successfully recently.

Writes a summary to the GitHub step summary, opens (or comments on) a
"Bot health alert" issue when something is wrong, and exits 1 so GitHub also
emails the failure. Never prints credentials.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import httpx
from forecasting_tools import MetaculusClient

TOURNAMENTS = ["fall-futureeval-2026", "minibench"]
GRACE_HOURS = float(os.getenv("SUPERVISOR_GRACE_HOURS") or 3)
MIN_CREDIT_USD = float(os.getenv("SUPERVISOR_MIN_CREDIT_USD") or 15)
MAX_HOURS_WITHOUT_SUCCESS = float(os.getenv("SUPERVISOR_MAX_QUIET_HOURS") or 6)
WORKFLOW_FILE = "run_bot_on_tournament.yaml"
ISSUE_TITLE = "Bot health alert"

now = datetime.now(timezone.utc)
problems: list[str] = []
lines: list[str] = [f"# Bot health {now:%Y-%m-%d %H:%M} UTC", ""]


def aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# 1. Coverage --------------------------------------------------------------
client = MetaculusClient()
lines.append("## Coverage")
for slug in TOURNAMENTS:
    try:
        questions = client.get_all_open_questions_from_tournament(slug)
    except Exception as exc:
        problems.append(f"Could not list `{slug}` questions ({type(exc).__name__})")
        continue
    due = [q for q in questions
           if aware(q.open_time) is None
           or now - aware(q.open_time) >= timedelta(hours=GRACE_HOURS)]
    missing = [q for q in due if not q.already_forecasted]
    lines.append(f"- `{slug}`: {len(questions)} open, {len(due)} past {GRACE_HOURS:g}h, "
                 f"{len(due) - len(missing)} forecast, **{len(missing)} missing**")
    for q in missing[:15]:
        lines.append(f"  - missing: [{q.question_text[:90]}]({q.page_url})")
    if missing:
        problems.append(f"{len(missing)} open `{slug}` question(s) without our forecast")

# 2. Budget ----------------------------------------------------------------
lines += ["", "## OpenRouter credit"]
raw_key = os.getenv("OPENROUTER_API_KEY", "")
found_keys = re.findall(r"(?<![0-9A-Za-z])sk-or-v1-[0-9a-fA-F]{64}(?![0-9a-fA-F])", raw_key)
key = found_keys[0] if len(found_keys) == 1 else raw_key.strip()
try:
    data = httpx.get("https://openrouter.ai/api/v1/key",
                     headers={"Authorization": f"Bearer {key}"}, timeout=30).raise_for_status().json()["data"]
    remaining = data.get("limit_remaining")
    usage = data.get("usage")
    lines.append(f"- used: ${usage:.2f}" if isinstance(usage, (int, float)) else "- used: unknown")
    if remaining is None:
        lines.append("- remaining: no limit reported")
    else:
        lines.append(f"- remaining: **${remaining:.2f}**")
        if remaining < MIN_CREDIT_USD:
            problems.append(f"OpenRouter credit low: ${remaining:.2f} left (alert below ${MIN_CREDIT_USD:g})")
except Exception as exc:
    problems.append(f"OpenRouter key check failed ({type(exc).__name__})")

# 3. Liveness --------------------------------------------------------------
lines += ["", "## Forecasting workflow"]
repo = os.environ["GITHUB_REPOSITORY"]
gh = httpx.Client(base_url="https://api.github.com", timeout=30, headers={
    "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
    "Accept": "application/vnd.github+json"})
try:
    runs = gh.get(f"/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/runs",
                  params={"per_page": 50}).raise_for_status().json()["workflow_runs"]
    day = [r for r in runs if datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) > now - timedelta(days=1)]
    ok = [r for r in day if r["conclusion"] == "success"]
    bad = [r for r in day if r["conclusion"] == "failure"]
    last_ok = next((r for r in runs if r["conclusion"] == "success"), None)
    lines.append(f"- last 24h: {len(day)} runs, {len(ok)} ok, {len(bad)} failed")
    if last_ok:
        age = now - datetime.fromisoformat(last_ok["created_at"].replace("Z", "+00:00"))
        lines.append(f"- last success: {age.total_seconds() / 3600:.1f}h ago ({last_ok['html_url']})")
        if age > timedelta(hours=MAX_HOURS_WITHOUT_SUCCESS):
            problems.append(f"No successful forecasting run for {age.total_seconds() / 3600:.1f}h")
    else:
        problems.append("No successful forecasting run found")
    if bad:
        lines.append(f"- latest failure: {bad[0]['html_url']}")
        if len(bad) >= 3:
            problems.append(f"{len(bad)} failed forecasting runs in the last 24h")
except Exception as exc:
    problems.append(f"GitHub run check failed ({type(exc).__name__})")

# Report -------------------------------------------------------------------
lines += ["", "## Verdict", *(f"- ❌ {p}" for p in problems)] if problems else ["", "## Verdict", "- ✅ all good"]
report = "\n".join(lines)
print(report)
summary = os.getenv("GITHUB_STEP_SUMMARY")
if summary:
    with open(summary, "a", encoding="utf-8") as fh:
        fh.write(report + "\n")

if problems:
    try:
        owner = repo.split("/")[0]
        found = gh.get(f"/repos/{repo}/issues", params={"state": "open", "per_page": 50}).raise_for_status().json()
        issue = next((i for i in found if i["title"] == ISSUE_TITLE and "pull_request" not in i), None)
        if issue:
            gh.post(f"/repos/{repo}/issues/{issue['number']}/comments", json={"body": report}).raise_for_status()
        else:
            gh.post(f"/repos/{repo}/issues", json={"title": ISSUE_TITLE, "body": report,
                                                   "assignees": [owner]}).raise_for_status()
    except Exception as exc:
        print(f"Could not open alert issue ({type(exc).__name__})", file=sys.stderr)
    sys.exit(1)

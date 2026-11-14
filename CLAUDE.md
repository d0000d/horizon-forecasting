# Horizon forecasting bot: instructions for any assistant working here

Domagoj owns this repo. He is not a programmer: he reads Croatian summaries on his
phone and cannot review code. Explain results in plain Croatian, short, with numbers.

## What runs

- `main.py` is the live bot. It forecasts on three Metaculus tournaments:
  `fall-futureeval-2026` (the one with the leaderboard), `minibench`, `market-pulse-26q4`.
- `.github/workflows/run_bot_on_tournament.yaml` runs it. Each run polls every 10
  minutes for about 5h45m and, in its last step, starts the next run. The hourly
  cron is only a backstop: GitHub fires it about every 8 hours for this repo.
- `supervisor.py` (workflow `supervisor.yaml`) checks coverage, credit and whether
  a watch run is alive, and opens a "Bot health alert" issue when something is wrong.
- `review_bot.yaml` scores resolved questions weekly. It only reads; it costs nothing.
- The old "Horizon" pipeline (`horizon_cloud.yaml`, `horizon_inventory.yaml`,
  `main_with_no_framework.py`, `review_recovery.py`) is paused. Leave it alone.

## Why the score is what it is

Fall questions are open for only a few hours and the tournament score is a sum over
questions, so a missed question is lost for good. The -37 score on the leaderboard
comes from six questions the old Horizon pipeline forecast before 25 October; the
current bot's forecasts had not resolved as of 14 November 2026. Do not judge or
retune the current bot until the weekly review shows resolved questions it forecast.

## Rules

1. Never break watching. Before changing `main.py` or the tournament workflow, ask
   what happens if the new code throws: a question must still get a forecast. New
   research or model calls go inside `try/except` with a fallback, like
   `_targeted_web_research`.
2. Money. The OpenRouter key is funded by Metaculus and is finite (about $59 left on
   14 November). `Test Bot` in its default mode forecasts ten test questions and
   costs real credit: run it only when a change cannot be checked another way.
   `Test Bot` with `mode=probe_research` is one cheap call. Never run `python main.py`
   locally or add a workflow trigger on `push`.
3. Models. This key does not serve every model: Perplexity Sonar returns NotFound.
   `openrouter/openai/gpt-5-mini:online` was verified for web research. Verify any
   new model with the probe before relying on it.
4. Secrets are write-only in GitHub. You cannot read them and must not print them.
5. Metaculus is not reachable from an assistant's sandbox. Anything that needs its
   API has to run in GitHub Actions (supervisor, review, the bot itself).
6. Small, single-purpose commits with the reason in the message. After a push that
   changes bot behaviour, start a new watch run
   (`gh workflow run run_bot_on_tournament.yaml -f watch_minutes=345`) and cancel the
   old one, because a run keeps the code it started with for six hours.

## How to answer "kako stojimo?"

1. `gh run list -L 10`: is a "Forecast on new AI tournament questions" run in
   progress or queued? If not, start one and say so.
2. Read the latest "Bot health supervisor" log: rank, score, coverage, missing
   forecasts, credit left.
3. In the last finished watch run's log, count `Posted prediction` and any
   `WARNING`/`ERROR` lines other than probability normalisation.
4. Report: watching yes/no, new forecasts, rank and score, credit, anything broken.
   Say plainly what you did not check.

## Open decisions that are Domagoj's

- Metaculus Cup: `run_bot_on_metaculus_cup.yaml` points at the finished summer cup
  (33021); the fall cup is 33108. Switching would spend credit re-forecasting every
  open question every two days. Do not switch without his answer.
- Other tournaments he joined: he has not named them yet. Ask, do not guess slugs.

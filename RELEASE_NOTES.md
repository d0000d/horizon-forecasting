# Horizon Council v0.2.0-alpha.1

Experimental release for controlled binary forecasting trials. Forecast accuracy
has not been established. This release does not enable autonomous tournament publishing.

## Changes

- Independent initial forecasting perspectives, structured resolution analysis,
  evidence selection, deterministic aggregation and adaptive adversarial review.
- Two predetermined AskNews searches: latest developments and criteria-focused
  background, with preserved responses, duplicate article removal and partial-failure reporting.
- Explicit OpenRouter transport, conservative cost reservations and local preflight
  checks that catch incompatible price limits before paid calls.
- At-most-once attempt tracking, closing-time guards and separate forecast/comment
  submission states. Existing attempts no longer consume the new-question limit.
- Frozen-outcome evaluation with coverage, Brier score, log loss, calibration bins,
  paired event-cluster bootstrap intervals and visible abstention costs.
- A credential-free example model configuration and offline test suite.

## Validation and limitations

82 offline tests pass locally on Python 3.12. The release workflow runs the same
suite on Python 3.11 before publishing. A live-model synthetic fair-coin fixture
completed both forecast perspectives and aggregation at 0.5, with status `review`;
this checks execution, not predictive accuracy or publication eligibility.

The real Metaculus/AskNews/OpenRouter test on 25 September retrieved sources and
completed resolution analysis and evidence selection. It abstained because no
retrieved source passed relevance selection. No forecast was published.
This remains a research-coverage limitation, not a successful real-world forecast.

The model price example was checked on 24 September 2026; recheck before use.
Prices and access can change. Per-question EUR 0.05, per-run EUR 0.25 and per-ledger
EUR 10 limits apply to model calls. AskNews quotas are separate, and separate
state directories have separate cost ledgers. Use one persistent state directory
for an ongoing deployment. Unknown model costs remain reserved.

Only standalone binary questions are supported. Predictive accuracy, full type
coverage, production scheduling and daily report delivery remain unvalidated.
Unit tests and synthetic model checks do not establish forecast skill.

## Quick start

Python 3.11 or newer:

```sh
python -m unittest discover -s tests -v
python -m pip install asknews==0.13.11 httpx
python -m horizon.runner --config configs/openrouter.example.json
```

Supply `METACULUS_TOKEN`, `OPENROUTER_API_KEY` and `ASKNEWS_API_KEY` through your
environment or secret manager. Never commit credentials. For one paid dry run:

```sh
python -m horizon.runner --config configs/openrouter.example.json --run --allow-paid-api --tournament bot-testing-area --state work/council-runner --max-questions 1
```

Publishing requires the separate `--publish` flag. This alpha is intended for
dry runs. Existing legacy workflow files are not deployment of Council.

Research strategy reference: [Metaculus official template](https://github.com/Metaculus/metac-bot-template/blob/main/main_with_no_framework.py).

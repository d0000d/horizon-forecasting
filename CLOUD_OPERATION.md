# Horizon cloud operation

The scheduled workflow runs on GitHub-hosted Ubuntu, every two hours at minute 17
(UTC). GitHub schedules can be delayed; this is polling, not an instant alert.
The owner does not need to leave their computer running for the cloud job.

The workflow checks out a reviewed, pinned commit. It never pushes code or tags.
The manual mode is observe, dry-run or publish. Scheduled mode defaults to observe
unless the HORIZON_MODE repository variable is explicitly configured.

Required existing repository secrets: METACULUS_TOKEN and OPENROUTER_API_KEY.
Paid research also needs ASKNEWS_API_KEY. Missing keys are shown as blockers and
do not trigger paid model calls. Adding a key to GitHub Actions gives workflows
in this repository access to that service; do not publish plaintext credentials.

Only standalone binary questions can be forecast. Other question types are
listed as unsupported. Forecasts are published only in publish mode and only
when the existing Council validation returns ok. Abstain/review results are
never converted into a forecast by a human or by this scheduler.

Operational state is restored from the immediately preceding successful run.
Missing/expired state, a cancelled or failed predecessor, or a manual rerun
halts processing rather than resetting the spending ledger. Reconcile state
before restarting after a failure. Keep the schedule active within artifact
retention (90 days). Only IDs, statuses and spending-ledger state are exported;
research responses, model traces, credentials and forecast memory stay off the
public artifact. Cloud forecast reasoning is therefore not retained after the
ephemeral runner exits; this is a limitation for later forensic evaluation.

Cloud model limits: EUR 0.05/question, EUR 0.25/run, EUR 10/cloud ledger.
At most three new binary attempts per run. AskNews quotas are separate. Local
test ledgers are separate from the cloud ledger; do not run parallel production
copies. Unknown model costs stay reserved. Existing attempts are not retried.

Each run produces a GitHub summary and a horizon-operational-state artifact.
New questions and a daily summary create GitHub issues assigned to the owner.
The owner receives GitHub notifications; email delivery depends on their GitHub
notification settings. Checks occur every two hours, not immediately at posting.
Neither code publication nor passing unit tests establishes predictive accuracy.

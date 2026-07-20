# Operating Rune's daily briefing

The production loop has one job: create a validated plan from the previous
local calendar day. It does not run review, grading, old CEO operations, or any
planned agent.

## Connect Microsoft Calendar

Create a public Microsoft application that allows device-code/public-client
flows and delegated `Calendars.Read`. Put its id in the gitignored
`state/pulse.json`:

```json
{
  "outlook": {
    "client_id": "<Azure application id>",
    "tenant": "common",
    "timezone": "SE Asia Standard Time"
  }
}
```

Sign in once:

```text
python dashboard/pulse.py --outlook-login
```

Start or restart `python dashboard/serve.py`. The Microsoft Calendar overview
card should show `synced` and the next event. When Graph is temporarily
unavailable, Rune keeps the last good events and labels them `cached` instead
of clearing the card.

Deterministic offline verification:

```text
python dashboard/pulse.py --selfcheck
```

## Generate a briefing manually

Windows:

```text
briefing.cmd
briefing.cmd --model fable --effort high
briefing.cmd --model gpt-5.6-sol --effort max
briefing.cmd --more
```

Direct CLI:

```text
python daily_briefing.py scheduled
python daily_briefing.py generate --date yesterday
python daily_briefing.py generate --date 2026-07-13 --json
python daily_briefing.py --summary
python daily_briefing.py --selfcheck
```

Useful generation flags:

| Flag | Meaning |
|---|---|
| `--model fable|gpt-5.6-sol` | Select the brainstorming provider. |
| `--effort low|medium|high|xhigh|max` | Select planning depth. |
| `--more` | Append another validated three-priority batch. |
| `--force` | Replace the primary result even when that source day already exists. |
| `--root <path>` | Override configured discovery roots; repeat to supply several. |
| `--json` | Print the persisted result as JSON. |

Without `--more` or `--force`, a successful primary result for that source date
is returned unchanged. A run needs at least three discoverable Git repositories.
The configured model CLI must be installed and authenticated.

## Install the 09:30 Windows schedule

Run the following PowerShell from the repository root. It creates an
interactive-user task so the model CLIs can read the same user profile and auth
as manual runs:

```powershell
$root = (Resolve-Path .).Path
$action = New-ScheduledTaskAction `
  -Execute "$env:SystemRoot\System32\cmd.exe" `
  -Argument ('/d /c ""{0}\briefing.cmd""' -f $root) `
  -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At 9:30AM
$settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal `
  -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "Rune Daily Briefing" `
  -Action $action -Trigger $trigger -Settings $settings `
  -Principal $principal -Description "Plan yesterday's three highest-leverage repository changes at 09:30 local time." -Force
```

Verify the trigger and next run:

```powershell
Get-ScheduledTask -TaskName "Rune Daily Briefing" |
  Select-Object TaskName, State, Actions, Triggers
Get-ScheduledTaskInfo -TaskName "Rune Daily Briefing" |
  Select-Object LastRunTime, LastTaskResult, NextRunTime
```

Windows Task Scheduler interprets `9:30AM` in the machine's local timezone.
The `StartWhenAvailable` setting runs a missed briefing after sleep or shutdown.
The dashboard server is a second recovery path: on boot and every minute it
checks the authoritative 09:30 cycle, launches one external catch-up process if
needed, and honors the durable retry timestamp after failure.

For cron-based hosts, `loop.sh` is the equivalent wrapper:

```cron
30 9 * * * /absolute/path/to/agentic_os/loop.sh
```

## Dashboard controls

The first generation button posts yesterday, selected model, and selected
effort to `/api/briefing/generate`. Once a primary batch exists it becomes
**Generate 3 more**. The request queues an in-process job; polling
`GET /api/briefing` reads status and the last good snapshot without rescanning
repositories or calling a model.

The freshness strip shows the saved plan day, last attempt, next scheduled
refresh or retry, and whether automatic catch-up is active. **Ensure current**
checks the authoritative 09:30 cycle. If that cycle is missing or overdue, it
queues one external background model run; if the briefing is current, already
running, or waiting for its scheduled retry, it reports that state without
starting a duplicate. The control says explicitly whether a run was queued.

**View more** reveals each priority's CEO plan and planned agents. Changing an
agent's model or effort posts to `/api/briefing/agent` and changes plan metadata
only. Supported agent models are Haiku, Sonnet, Opus, Fable 5, and GPT-5.6 Sol.

The solid **Run this plan** action is the protected default. It starts the CEO
harness with native permission handling. The outlined **Run · skip permissions**
action confirms every launch, bypasses permission prompts for that run only,
and executes the saved role cards directly. Claude cards use
`--dangerously-skip-permissions`; GPT-5.6 Sol cards use Codex `--yolo`. Provider,
model, repository, and argv are resolved from the stored briefing on the server,
not trusted from browser input. These workers have no console window; monitor or
stop them in Mission Activity. The chosen provider policy is preserved for
initial, resumed, and bounded-recovery workers. This does not disable Rune's
independent Maestro guard for protected outward, destructive, spending, or
soul-write actions.

When a role reaches `waiting_permission`, Mission Activity remains actionable
after its worker exits. **Allow & resume** authorizes only the displayed request,
mission, and role for a short window; **Retry after fixing** adds no permission;
**Deny & skip** skips only that role. Every action includes the server-issued
request ID, so a delayed click cannot approve a newer boundary. Credential and
login requests cannot be approved in the dashboard: fix them outside Rune, then
use Retry.

Mission Activity **Open** targets the mission id rather than only changing the
route. It collapses the tray, opens Agent console (including its completed
drawer), expands the matching record, and moves keyboard focus to it. This also
works when Agent console is already the current route.

If a Claude worker reports a weekly/capacity limit, the harness checks the local
Codex CLI. When it is installed, authenticated, and still has 5-hour and weekly
headroom, Rune performs one persisted Claude → Codex switch for that unfinished
role using GPT-5.6 Sol. The repository, role assignment, and current run's
permission mode are preserved; the Claude session id is retained only as audit
evidence and is not passed to Codex. Agent console displays the handoff as
running, completed, or failed. An unavailable/exhausted Codex account is shown
instead of causing an unbounded provider loop.

### Verify brain retrieval

Open **Brain â†’ Recall evidence** to inspect every deterministic lookup. A
receipt identifies the mission/route, query and corpus fingerprints, exact card
IDs and scores, ranking/diversity guards, and the prompt count plus estimated
context tokens actually inserted. A `miss` and a ranker `error` are recorded as
deliberately as a hit. Later mission status is correlation only: the interface
does not claim that the model obeyed a card or know how many tokens would have
been spent without it.

Enter a problem in Brain search and choose **Verify retrieval** to replay the
production ranker without calling a model. This creates a verification-only
receipt and does not increment reuse telemetry. Storage health shows active
count/bytes and budgets, quarantine/archive utilization, below-threshold legacy
cards, merges, and recent admission decisions with their quality reason codes.

For an offline check:

```text
python hermes/hermes.py stats --json
python hermes/hermes.py query --json "daily briefing stale server refresh"
```

## Complete and deliver a plan

When every role succeeds, the run is removed from the active CEO queue and
retained under **Agent console → Completed & delivery**. Recoverable failures
stay in the active queue. Daily briefing cards that already succeeded move to
the **Completed plans** drawer instead of being suggested as unfinished work;
use **Review delivery** to open their mission or **Run again** for an explicit
new execution.

Delivery is intentionally sequential:

1. **Review changes** records Git status, changed paths, tracked diffs, bounded
   untracked source previews, and `git diff --check` evidence, then adds an
   advisory AI review: a verdict ("looks right" / "needs your eyes"), a
   plain-language summary, and per-file issues rendered above the raw report
   next to a visual diffstat. The AI opinion informs you; it never blocks.
2. **Run tests** chooses a repository-native test command on the server,
   preferring the project's own environment (root pytest/unittest markers, a
   verified Poetry env, or a nested project's `.venv`). Drop a
   `.rune-test.json` at the repository root — `{"argv": ["poetry", "run",
   "pytest", "-q"], "cwd": "apps/api"}` — to pin the command explicitly. A test
   run that changes the *reviewed* files fails the gate; unrelated churn (logs,
   caches, sync noise) neither fails tests nor invalidates the review.
3. **Commit** is enabled only when review and tests still match the reviewed
   files. Git hooks remain enabled, and exactly the reviewed, mission-attributed
   path set is committed — files that appeared after the review, and files that
   were already dirty before the mission started, can never ride along.
4. **Push** first shows the server-resolved remote, branch, and HEAD. Confirming
   consumes a short-lived one-use token and sends an explicit non-force refspec.

If the reviewed files change underneath a review, the step turns **stale** with
the reason kept on the card — run Review again to see the current diff. Any
failed step offers **Fix with agent**: one solo mission is started in the
mission's repository with the persisted failure evidence; it diagnoses,
repairs, and re-runs the failing check itself, but never commits or pushes.

A repository that was already dirty at mission start no longer blocks delivery.
The review lists those files under **Pre-existing operator changes**, and
commit stages only the mission-attributed paths — your work in progress is
never mixed into the mission's commit. If *every* reviewed change overlaps
pre-existing work, commit stays blocked with that exact reason; commit the
operator work manually in Git. Only legacy runs (finished before Git
attribution was captured) and runs without a recorded working directory keep
automatic commit disabled.

## Failure behavior

- A file lock rejects overlapping generations.
- Scheduled attempts are recorded in `state/briefing-status.json`. A failed
  attempt retains the last good plan and becomes eligible for retry 15 minutes
  after that attempt finishes.
- Invalid structured output is retried once, then reported without overwriting
  the prior briefing.
- A failed Microsoft refresh retains the last good per-service cache and marks
  it stale.
- Repository evidence is untrusted prompt data; sensitive-looking paths are
  omitted and raw evidence is not persisted.
- The dashboard server is loopback-only, rejects cross-origin POSTs, caps JSON
  request bodies, and blocks private state from static serving.

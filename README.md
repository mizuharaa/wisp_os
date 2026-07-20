# Rune — Agentic OS

Rune is a local control surface for memory, guarded automation, specialist
agents, account health, Microsoft Calendar, and an executive daily briefing.
It uses Python 3's standard library and binds its dashboard to `127.0.0.1`.
MIT licensed.

## Quickstart

```text
python bootstrap.py
python bootstrap.py boot
python desktop.py
# or: python dashboard/serve.py
```

Open `http://127.0.0.1:8817/dashboard/` when running the server directly.

The dashboard starts with an operator overview:

- **Mission activity** is a bounded, collapsible tray. Running work always
  exposes Stop/Open; recoverable work exposes Continue; finished work can be
  dismissed without moving the rest of the page. **Open** routes to the exact
  mission, expands completed history when needed, and focuses the matching
  record even when Agent console is already open.
- **Microsoft Calendar** shows the next Outlook event on Overview and provides
  full Month, Week, Day, and Agenda views on the Calendar route.
- **Daily briefing** shows three specific, high-impact changes selected from
  three different repositories.
- **Automation radar** ranks review-first workflow suggestions from the same
  deterministic coach available at `/api/workflows`.

## Operator workbench

Rune is organized around four operator verbs: launch, monitor, intervene, and
recover. Mission state is never color-only, every overlay closes with Escape,
and the global mission tray is internally scrollable instead of appending an
unbounded conversation to the document. **Stop active** terminates running
process trees; **Clear completed** only clears presentation state.

The interface tokens and interaction contract live in `tokens.css` and
`design.md`. The palette uses warm paper surfaces with high-contrast royal
plum actions and separate success, retry, permission, and failure treatments.

## Microsoft Calendar

Rune uses Microsoft Graph device-code authentication with the delegated
`Calendars.Read` scope. Add the public-client application id to the gitignored
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

Then sign in once and restart the dashboard:

```text
python dashboard/pulse.py --outlook-login
python dashboard/serve.py
```

The pulse refreshes Outlook in the background and atomically retains the last
good calendar snapshot when a refresh fails. It keeps 35 days behind and 92
days ahead, including event end times and source links. The same data powers
the overview card, all four Calendar views, `/api/pulse`, and the range-aware
`/api/calendar?start=YYYY-MM-DD&days=N` endpoint. Navigation outside the
synced window stays honest: the UI shows a coverage notice rather than
inventing empty availability.

## Recovery and workflow learning

CEO planning and workers classify failures before acting. Transient transport
or capacity failures retry with bounded, interruptible backoff. Missing
credentials and explicit operator requests pause as `waiting_permission`, even
when a provider exits normally instead of returning an error. Mission Activity
persists the exact request and offers **Allow & resume**, **Retry after fixing**,
or **Deny & skip** after the worker exits. Allow is bound to the displayed
request, mission, and role; stale clicks cannot approve a newer request.
Credential/login requests cannot be authorized in Rune and remain Retry-only.
Ordinary task failures may receive
at most two local, reversible fixer cycles, after which the original role must
pass again as verification. Stops terminate the whole process tree on Windows
and POSIX, and a late worker response cannot overwrite a stopped state.

When a Claude worker returns an actual weekly/capacity-limit error, Rune can
switch that unfinished role to the local Codex CLI once. The fallback requires
an installed, authenticated Codex CLI with available 5-hour and weekly
capacity, uses GPT-5.6 Sol, preserves the mission worktree and safe/skip
permission mode, and never reuses the incompatible Claude session id. The role
shows the Claude → Codex handoff and result in Agent console. If Codex is not
ready, Rune records why and keeps the normal retry budget bounded.

Automatic recovery never approves destructive, outward-facing, credential, or
spending decisions. Completed roles and resumable worker sessions are retained
across recovery and server restart. Only verified, nontrivial, secret-redacted
repair evidence is eligible for a Hermes note.

Two candidate skills support this loop:

- `recovery-supervisor` explains and performs bounded mission recovery.
- `workflow-coach` analyzes repeated actions, short sequences, and
  failure/recovery patterns. Suggestions require at least three observations,
  include evidence/confidence, and are always advisory-only.

Run the coach directly with:

```text
python skills/workflow-coach/scripts/analyze.py
python skills/workflow-coach/scripts/analyze.py --json
```

## Brain proof and retention

Rune now queries Hermes deterministically before CEO planning, direct workers,
dashboard chat, and ordinary Claude prompts. The model does not decide whether
to search. Every attempt writes a bounded, secret-safe receipt containing the
query and corpus fingerprints, ranked card IDs and scores, ranking guards,
prompt destination, and estimated context inserted. Mission outcomes are linked
later as correlation only. Receipts prove retrieval and prompt insertion; they
cannot prove that a model followed a card or measure counterfactual tokens saved.

The Brain page exposes those receipts and has **Verify retrieval**, which runs
the production ranker without calling a model or counting the check as reuse.
`GET /api/brain` returns the same proof plus storage health, and
`POST /api/brain/query` performs the no-model verification.

Hermes admits reusable evidence rather than every transcript. Verified root
causes, recipes, mechanisms, and guardrails score positively; raw logs, one-off
status, vague fixes, unresolved failures, and dumps are quarantined. Trusted
near-duplicates reinforce one card, while low-quality duplicates cannot inflate
retention. Hit count is telemetry and has zero ranking weight. Active cards,
quarantine, usage, receipt, and compressed archive stores all have explicit
count/byte limits; once preservation space is exhausted, new candidates are
rejected explicitly instead of growing the SSD indefinitely.

Inspect the policy from the CLI:

```text
python hermes/hermes.py stats --json
python hermes/hermes.py query --json "a problem to reproduce"
python hermes/hermes.py note "problem" "verified reusable solution" --tags debugging,reusable --source manual --json
```

## Daily briefing

The briefing analyzes the previous **local calendar day**. Git history from all
branches, changed paths and stats, recent working-tree signals, and bounded
TODO/README context are private evidence for planning. Raw evidence and commit
messages are not persisted or rendered.

Each generated batch contains exactly three priorities, one per repository.
Every collapsed card shows the outcome and first move. **View more** reveals a
short CEO plan, definition of done, and two to four animated role cards with a
clear mission and deliverable. Agent cards are plans only; generation never
executes or edits the selected repositories.

Use Fable 5 or GPT-5.6 Sol for the brainstorm and choose an effort from `low`,
`medium`, `high`, `xhigh`, or `max`. Each planned agent can then have its own
model and effort changed from the card. **Generate 3 more** appends another
validated batch without repeating an existing repository/title pair.

An expanded priority has two deliberately separate execution controls.
**Run this plan** keeps native permission handling and lets the CEO restaff the
saved suggestions. **Run · skip permissions** asks for confirmation on every
click, then runs the saved cards headlessly with their selected providers:
Claude models receive `--dangerously-skip-permissions`, while GPT-5.6 Sol uses
Codex with `--yolo`. This bypass applies to that run only. The server derives
the provider, model, working directory, and command from the saved plan; the
browser cannot submit them. Progress and Stop remain available in Mission
Activity. The selected provider policy remains authoritative for initial,
resumed, and recovery workers. Rune's independent Maestro guard still pauses
protected outward, destructive, spending, and soul-write actions; Mission
Activity can grant a short-lived approval scoped to that exact mission, role,
and recorded action.

Successful CEO runs leave the active queue automatically and remain in
**Completed & delivery** history; failed, stopped, exhausted, and
permission-blocked runs stay active so they can be resumed. Completed briefing
priorities likewise move into **Completed plans**, where they can be reopened or
run again. Each completed mission has an explicit **Review → Tests → Commit →
Push** lane. Review shows a redacted tracked/untracked change report, tests use a
server-detected project command, commit includes only server-attributed paths,
and push requires a fresh second confirmation and never force-pushes. If a
repository was already dirty when the mission started, review and tests remain
available but automatic commit is disabled to avoid mixing unrelated work.

Run it on demand:

```text
briefing.cmd
python daily_briefing.py scheduled
python daily_briefing.py generate --date yesterday
python daily_briefing.py generate --date yesterday --model gpt-5.6-sol --effort max
python daily_briefing.py generate --date yesterday --more
```

The production schedule is **09:30 local time every day**, using Windows Task
Scheduler to run `briefing.cmd`. The shared `scheduled` command freezes the
source date belonging to the latest 09:30 cycle, so a delayed run cannot drift
across midnight. The dashboard server also checks on boot and every minute for
a missed cycle. Attempts are durable, failures keep the last good plan visible,
and automatic retries wait 15 minutes. The dashboard's **Ensure current** action
uses that same deduplicated catch-up path: it queues one background model run
only when the current cycle is missing or overdue, and otherwise reports the
fresh, running, or retry-wait state without starting a duplicate. See
`OPERATOR.md` for setup and verification.

## Core layers

| Layer | Location | Purpose |
|---|---|---|
| Identity | `soul/` | Hand-maintained mission and operating character. Automated writes are guarded. |
| Rules | `.claude/hooks/` | Approval guard plus the append-only event wire. |
| Skills | `skills/` | Earned capabilities, recovery guidance, and advisory workflow coaching. |
| Agents | `.claude/agents/` | Specialist roster used only when work is deliberately delegated. |
| Memory | `memory/`, `hermes/` | Obsidian pipeline and reusable solved-problem notes. |
| Morning data | `dashboard/pulse.py`, `daily_briefing.py` | Calendar sync and the plan-only, cross-repository briefing. |
| Console | `dashboard/` | High-contrast vanilla workbench with a Python stdlib API server and shared recovery runtime. |

Operational execution events still flow through `state/events.jsonl`. Briefing
generation is intentionally separate: it persists its validated plan in
`state/briefing.json` and does not manufacture execution events. An explicit
Run action creates a normal tracked CEO mission and emits its usual activity.

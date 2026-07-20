# Architecture

This document describes Rune's operator workbench, recovery runtime, calendar,
and plan-only daily briefing. It replaces the retired review/grading pipeline
that treated activity logs as the product.

## Data flow

```text
Microsoft Graph ──> dashboard/pulse.py ──> state/pulse-cache.json
                                             │
                                             ├─> GET /api/pulse
                                             └─> GET /api/calendar
                                                        │
                                                        v
                                                Calendar dashboard card

sibling Git repos ──> private yesterday evidence ──> Fable 5 or GPT-5.6 Sol
                                                        │
                                              validate exactly 3 plans
                                                        │
                                                        v
                                               state/briefing.json
                                                        │
                                              GET /api/briefing
                                                        │
                                                        v
                                      collapsed priority and agent cards
```

Neither path executes repository work. Calendar is read-only. Briefing
generation collects evidence, asks for structured plans, validates them, and
only then replaces the last good briefing atomically.

CEO and orchestration missions are a separate execution path:

```text
command bar -> dashboard/ceo.py -> planner -> role worker
                                      |             |
                                      |      classified failure
                                      |             |
                                      +-- persisted run state
                                                    |
                         transient -> bounded retry |
                         permission -> operator wait|
                         task -> safe fixer (<=2) -> original-role verification
                                                    |
                     success -> completion verifier (accept / one revise)
                                                    |
              terminal role failure -> one CEO replan from the role ledger
                                                    |
                                   verified learning -> Hermes (best effort)
                                                    |
                       delivery lane: review -> test -> commit -> push
```

## Calendar path

`dashboard/pulse.py` authenticates a public Microsoft application with the
device-code flow and requests `offline_access Calendars.Read`. It calls Graph's
`calendarView` endpoint with explicit local offsets and an Outlook timezone
preference, follows bounded same-host pagination, converts event times, and
refreshes in a background thread.

Outlook has its own freshness timestamp. A GitHub or Gmail refresh cannot make
old calendar data appear current. Cold starts hydrate from
`state/pulse-cache.json`; transient failures preserve the last good events and
add stale/error metadata. A cross-process lock serializes cache merges and
rotated-token updates before atomic replacement, so simultaneous Outlook and
Spotify refreshes cannot erase one another.

The overview card prefers live `/api/pulse` data. `/api/calendar` accepts a
bounded start/day range and the Calendar route renders Month, Week, Day, and
Agenda from the same 35-days-back/92-days-forward snapshot. Event ids, end
times, all-day state, location, and safe source links survive normalization.
The calendar included in `/api/briefing` uses the same cache, with an optional
existing ICS subscription as fallback.

## Mission recovery path

`dashboard/runtime.py` is the shared safety layer for CEO roles and conductor
loops. It owns process-tree termination, failure classification, interruptible
backoff, protected-action preflight, secret-safe excerpts, and compact recovery
evidence.

`dashboard/ceo.py` persists attempts, planning history, recovery history,
detail, and next action in `state/ceo/<cid>.json`. Planner retry is limited to
transient, empty, or malformed responses. Worker retries are limited to
classified transport/capacity failures. A task-class failure can launch at
most two native-permission fixer cycles; the original role is always the
verifier. Permission, credential, destructive, outward, spending, and access
decisions remain operator-gated.

A role that reports success must pass a tier-1 completion verifier before it is
marked done: a Haiku call judges the role's report against its mission brief
and either accepts or sends the role back once with concrete feedback. The
verdict is persisted on the role and shown in the console; an unreachable
verifier never blocks completion. When a role fails terminally in a delegated
mission, the CEO performs at most one mid-mission replan: it rereads the full
role ledger (every outcome plus failure evidence) and staffs a revised 1–3 role
tail with an explicit different-approach instruction. Done work is kept;
unfinished roles are superseded. `RUNE_DISABLE_VERIFIER=1` and
`RUNE_DISABLE_REPLAN=1` turn these model gates off for offline runs and tests.

New missions opt into restart recovery. On server boot, interrupted planning or
working states resume from persisted context; review and permission waits do
not. Stop is monotonic, kills descendants, and wins races with a late worker.

`dashboard/orchestrator.py` uses the same tree-kill and transient-retry
helpers for both worker and critic calls.

## Delivery lane

`dashboard/delivery.py` guards review → test → commit → push for completed
missions. Gate fingerprints are scoped to the reviewed path set: unrelated
worktree churn (logs, caches, sync noise) can neither invalidate a review nor
ride into a commit, because commit stages exactly the reviewed paths. A review
whose files changed underneath it persists as `stale` with a durable reason
instead of dead-ending in a transient error.

Review combines git hygiene checks with an advisory Haiku read of the diff
against the mission goal (verdict, plain-language summary, per-file issues; it
informs the operator and is never a gate; `RUNE_DISABLE_AI_REVIEW=1` disables).
Tests resolve the project's own interpreter — root pytest/unittest markers, a
verified Poetry environment, or a nested project's `.venv` — and a
`.rune-test.json` at the repository root (`{"argv": [...], "cwd": "optional"}`)
pins the command explicitly. A failed step exposes the `fix` action, which
spawns one solo fixer mission built server-side from the persisted failure
evidence; it may repair and re-run checks but never commits or pushes.

## Workflow suggestion path

`skills/workflow-coach/scripts/analyze.py` reads the append-only event wire,
redacts credential-shaped evidence, removes navigation noise, normalizes
volatile paths/ids, and looks for action families, adjacent sequences no more
than 15 minutes apart, and failure/recovery correlations. All candidates need
at least three observations. Missing input fails loudly and malformed input
counts remain visible.

`GET /api/workflows` exposes the top 12 cached candidates to Automation Radar
and reports the total. Its contract always includes
`advisory_only=true`, `review_required=true`, and `executed=false`.

## Briefing path

`daily_briefing.py` discovers up to 30 Git repositories beneath configured
roots. For the previous local day `[00:00, 00:00)`, it collects bounded signals:

- commits from all refs, changed paths, and diff totals;
- working-tree paths whose modification time falls in the source day;
- relevant TODO/README context from safe text files.

Sensitive-looking paths and linked, reparse-point, or hard-linked evidence
files are omitted. Repository text is marked as untrusted in the model prompt.
Evidence is never stored in `state/briefing.json`; only the validated decision
artifact is stored.

The planning model must return exactly three priorities that refer to three
different repository ids. Each priority contains a title, reason, outcome,
first move, two to six CEO steps, a definition of done, and two to four planned
agents. An agent has a role, icon, mission, deliverable, model, effort, and
`planned` status.

The validator rejects unknown or duplicate repositories, empty content,
unsupported models/efforts, repeated additional priorities, and malformed
agent plans. It also rejects secret-like output, absolute paths, and copied or
near-copied commit subjects. Generation retries malformed model output once. A
file lock stops
overlapping generations, and atomic replacement keeps the previous snapshot if
collection, model execution, or validation fails.

Primary generation is idempotent for a source date. `--more` appends a new
three-priority batch; `--force` replaces the primary result for that date. None
of these modes starts the planned agents.

## Models and controls

Briefing brainstorm providers:

- `fable` — Fable 5 through the Claude CLI in safe, tool-free mode.
- `gpt-5.6-sol` — GPT-5.6 Sol through Codex from an isolated temporary
  directory with shell, browser, app, computer, image, and multi-agent tools
  disabled.

Shared effort values are `low`, `medium`, `high`, `xhigh`, and `max`. Planned
agent cards additionally allow Haiku, Sonnet, and Opus. Updating a card changes
only its persisted plan metadata while its status remains `planned`.

## HTTP contract

| Method | Route | Result |
|---|---|---|
| GET | `/api/pulse` | Live/cached outside-world snapshot, including Outlook. |
| GET | `/api/calendar` | Normalized calendar events and freshness. |
| GET | `/api/workflows` | Ranked, evidence-backed workflow suggestions; never execution. |
| GET | `/api/ceo` | Persisted CEO missions, live state, attempts, and recovery history. |
| POST | `/api/ceo-action` | Stop, resume, archive, or resolve a gated role. |
| POST | `/api/ceo-delivery` | Guarded review/test/commit/two-step push, or `fix` to spawn a solo fixer. |
| GET | `/api/briefing` | Last good briefing, async job status, settings, and calendar. |
| GET | `/api/briefing/job` | Current in-process generation job. |
| GET | `/api/briefing/settings` | Default model, effort, and repository roots. |
| POST | `/api/briefing/generate` | Queue an asynchronous, plan-only generation. |
| POST | `/api/briefing/agent` | Change one planned agent's model and/or effort. |
| POST | `/api/briefing/settings` | Change briefing defaults. |

The server accepts object-shaped POST bodies up to 1 MiB and rejects
cross-origin requests. Static serving uses an explicit asset allowlist and
rejects Windows path aliases; the dashboard binds only to `127.0.0.1`.

## Presentation contract

The global Mission Activity tray is fixed, height-bounded, internally
scrollable, and collapsible with Escape. It groups prompt, latest reply, state,
and actions by mission instead of appending permanent chat bubbles. The
overview orders Microsoft Calendar before Daily briefing and follows it with
Automation Radar. Calendar is responsive down to 320px without hiding Today or
the four view selectors. Recovery and permission states use explicit labels in
addition to semantic color.

Dashboard polling and **Check now** only read the stored briefing. Repository
scans and model calls occur after an explicit generation request, the scheduled
09:30 command, or the server's boot/minute catch-up when that cycle is overdue.
Catch-up runs in an external process with a frozen source date and durable
failure/retry state.

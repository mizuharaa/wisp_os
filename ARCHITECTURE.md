# Architecture

The shared model/orchestration layer everything else builds on. Six components,
four data contracts. Contracts are minimal JSON — every module reads and writes
the same shapes onto the existing wire (`state/events.jsonl`), so nothing needs a
private format and the UI graph can render any of it without a translation step.

## Principles

- **One model door.** All model calls go through `model_client.py`. Model choice,
  token budget, effort, and refusal handling are decided there, once. The 1-week
  Opus upgrade is a single constant flip (`MODEL`).
- **Refusals are data, not crashes.** Every call returns a 200-shaped envelope
  with a `stopped_reason`. A loop that gets a refusal re-routes; it never exits.
- **Cheap-first escalation.** A haiku scan sizes work, opus plans, then the build
  is delegated to the tier that fits. Don't spend Fable on a lookup.
- **The wire is the truth.** Components communicate by appending events, not by
  calling each other. The dashboard (and the UI graph) read only the wire.

```
                         ┌───────────────────┐
      prompt / cron ────▶│ 1. Daily review   │  sizes the day, files tasks
                         │    loop           │
                         └─────────┬─────────┘
                                   │ task[]
                                   ▼
        ┌───────────────────────────────────────────┐
        │ 2. Agent orchestration / cards            │  escalate() -> roster
        │    (model_client.complete per role)       │  emits agent-card status
        └───────┬───────────────────────────┬───────┘
                │ result                     │ status
                ▼                            ▼
        ┌──────────────┐            ┌──────────────────┐
        │ 4. Grading / │◀───────────│ 3. Feedback-loop │  critic->doer until
        │    audit     │  grade[]   │    agent         │  the goal predicate
        └──────┬───────┘            └──────────────────┘
               │ audit[]
               ▼
        ┌──────────────┐   consumes tasks+grades   ┌──────────────────┐
        │ 5. Skill-    │──────────────────────────▶│ 6. UI graph      │
        │    tree tests│   (pass gates a skill)    │ (reads the wire) │
        └──────────────┘                           └──────────────────┘
```

---

## Data contracts

Four shapes, shared across all six components. Every field is JSON-native; ids
are short hex slugs (matching `state/ceo/<cid>.json`). Extra keys are allowed —
consumers read what they know and ignore the rest.

### `task`
The unit of work. Produced by the review loop, consumed by orchestration, graded
by grading/audit.
```json
{
  "id": "a1b2c3d4",
  "goal": "one-line what/why",
  "complexity": "trivial|light|hard|frontier",
  "status": "pending|running|blocked|review|done|failed",
  "depends_on": ["<task id>"],
  "created": "2026-07-13T10:00:00",
  "source": "daily-review|directive|feedback"
}
```
`complexity` feeds `model_client.escalate()`; `status` is the same vocabulary the
CEO roles already use, so the UI needs no mapping.

### `grade`
The verdict on a finished task. Produced by grading/audit and the feedback loop.
```json
{
  "task_id": "a1b2c3d4",
  "score": 0.0,
  "verdict": "pass|revise|fail",
  "rubric": ["criterion met/unmet ..."],
  "grader": "opus",
  "ts": "2026-07-13T10:05:00"
}
```
`score` is 0–1. `verdict: revise` is the signal the feedback loop iterates on;
`pass` is what gates a skill-tree test.

### `agent-card status`
One card per spawned role. Emitted on every state transition so the UI graph and
the dashboard render live.
```json
{
  "card_id": "a1b2c3d4:eng",
  "task_id": "a1b2c3d4",
  "role": "eng",
  "model": "fable|opus|sonnet|haiku",
  "effort": "low|medium|high|xhigh|max",
  "status": "pending|working|blocked|review|done|failed",
  "stopped_reason": null,
  "cost": 0.0,
  "ts": "2026-07-13T10:02:00"
}
```
`stopped_reason` is carried straight from `model_client.normalize()` — a card can
show "refused (cyber)" without the run having crashed.

### `audit log entry`
The append-only record of what happened and why. Written by grading/audit; the
UI graph replays it.
```json
{
  "id": "e5f6a7b8",
  "task_id": "a1b2c3d4",
  "actor": "grading|feedback|orchestration|review-loop",
  "action": "graded|escalated|refused|shipped|reverted",
  "detail": "one line",
  "refs": {"grade": "...", "card": "a1b2c3d4:eng"},
  "ts": "2026-07-13T10:06:00"
}
```
This is a typed superset of the existing `mirror.py` event, so entries land on
`state/events.jsonl` unchanged.

---

## Components

### 1. Daily review loop
Reads the inbox and open tasks at a cadence (cron or session boot), sizes each
into a `task` with a `complexity`, and files them on the wire. Idempotent: a task
already `done` is not re-filed. Output contract: `task[]`.

### 2. Agent orchestration / cards
For each runnable `task`, calls `model_client.escalate(task.complexity)` to pick
the roster tier, then `model_client.complete(...)` per role. Every transition
emits an `agent-card status`. Because `complete()` never raises, a role that gets
refused is marked `status: blocked, stopped_reason: refusal` and the rest of the
roster keeps running — the loop closes instead of hanging. This is the direct
successor to `dashboard/ceo.py`, now sharing the model door.

### 3. Feedback-loop agent
A critic→doer loop (the earned `loop-engineering` skill) bounded by a
max-iteration budget. Reads a `grade`; while `verdict == revise` and iterations
remain, it re-briefs the doer with the rubric gaps and re-runs. Emits a new
`grade` each pass and stops on `pass`, `fail`, or budget exhaustion. Never spins
forever — the budget is the ceiling.

### 4. Grading / audit
Grades a finished `task` against a rubric using an opus grader (a fresh context,
not self-critique), producing a `grade`, and writes an `audit log entry` for the
decision. Grading and audit are one component because every grade is an auditable
event — the score and the reasoning land together on the wire.

### 5. Skill-tree tests
Each skill has a checkable predicate. A skill advances (earns / stays active) only
when its associated `task`s carry a `grade` with `verdict: pass`. Consumes
`task` + `grade`; writes skill-registry transitions as `audit log entries`. This
is the enforcement arm of "skills are earned, not declared."

### 6. UI graph
Reads only the wire (`events.jsonl` + registries) and renders tasks, agent cards,
grades, and audit entries as a live graph — nodes are tasks/skills, edges are
`depends_on` and `refs`. It writes nothing. Any component's output is renderable
because they all speak the four contracts above.

---

## The model layer (`model_client.py`)

The foundation the six components share:

- `MODEL` — the model string, `claude-fable-5` today. **The single-constant swap
  path to Opus** is `MODEL = MODEL_IDS["opus"]`.
- `complete(prompt, ...)` — one Messages call that **always** returns a
  `normalize()` envelope (`status: 200`, `stopped_reason`, `text`, `usage`).
  Fable's thinking is always on, so depth is set via `effort`, not a thinking
  budget.
- `resolve_effort(effort, agentic_loop)` — the `low..max` ladder. `xhigh` is
  gated: allowed only when `agentic_loop=True`, else it degrades to `high`.
- `clamp_max_tokens(n)` — forces the per-call budget into 64K–100K.
- `escalate(complexity, agentic_loop)` — the tiering plan: haiku scan → opus plan
  → Sonnet/Opus/Fable delegate.

Run `python model_client.py` for the self-check (simulated refusal → 200 +
`stopped_reason`, plus the effort/token config assertions).

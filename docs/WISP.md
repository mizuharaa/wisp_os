# Wisp — Product Spec v2 (dev pivot, 2026-07-23)

**A local control plane for agentic development on Windows and WSL.**

Run many long-lived coding agents on your own machine with real operational
hygiene: spawn, watch, approve, stop, resume, audit, roll back. Wisp is the
thing everyone running Claude Code or Codex in a loop is currently improvising
with tmux and hope.

v1 of this spec chased a consumer assistant. That was two products sharing one
engine, and the consumer half was the unproven one. The dev half is what this
codebase has actually been doing in production since the Rune era. This spec
cuts the consumer surface from the roadmap — not from ambition, from the
roadmap.

## The person

A developer who runs agentic coding sessions daily — Claude Code, Codex, or
both — often several at once, on Windows or WSL. Their pain today:

- agents die silently mid-mission and nobody notices for an hour
- no approval gate between an agent and `git push --force`, spending, deletes
- no durable record of what an agent actually did
- crash/reboot means archaeology, not resume
- credentials are one global soup shared by every agent
- GUI automation (when agents must touch apps) is screenshot-and-pray

Success metric for the next six weeks: **fifteen people in this group who
would be annoyed if Wisp disappeared.**

## Positioning

- **Not** an orchestration framework (crowded) and **not** hosted
  observability (crowded). A *local* control plane with operational teeth,
  on the platform where tooling is weakest (Windows + WSL).
- **Not** privacy-and-local as the headline. On Windows there is no unified-
  memory floor; continuous local inference on a median laptop is a bad
  experience. Local models stay an *option* (routing cheap classification),
  never the pitch. The pitch is **structured control and honest reliability**:
  works 95% of the time and tells you truthfully when it didn't.
- The moat: a **UIA-native action runtime**. Windows UI Automation exposes a
  real accessibility tree — controls, values, invocations — as structured
  data. Acting on that tree instead of pixels is faster, cheaper, more
  deterministic, works unfocused, and is *verifiable*: assert the button was
  invoked and the field holds the value. Nobody has built an excellent
  UIA-native agent runtime. That attacks the weakest part of every
  screenshot-driven competitor.

## What already exists (the Rune inheritance)

| Control-plane need | Status in this repo |
|---|---|
| Spawn/monitor real agent sessions | `POST /api/spawn`, managed windows, focus/close, PID liveness |
| Long-running missions w/ worker+critic revision | `orchestrator.py` conductor loops |
| Mission planning, roles, delegation | `ceo.py` command bar → planner → role workers |
| Approval queue for destructive actions | guard hooks + `waiting_permission` + allow/retry/deny (request-id bound, stale clicks can't approve newer requests) |
| Resumability after crash/restart | recovery runtime: classified failures, bounded retries, `--resume`, stalled-state detection |
| Durable audit log | `state/events.jsonl` append-only wire + mission records |
| Rollback / safe delivery | delivery pipeline: scoped review → test → commit gates |
| Per-agent accounts | `CLAUDE_CONFIG_DIR` per spawn + usage tracking |
| Panic stop | `POST /api/stop-all` (process-tree kill) |

This is the product. The work is sharpening it, not building it.

## Architecture

```text
┌─ Electron shell (app/) ────────────────────────────────┐
│  dashboard window · tray · mini bar = approval island   │
└──────────────┬─────────────────────────────────────────┘
               │ 127.0.0.1:8817
┌──────────────▼─────────────────────────────────────────┐
│  Python engine (stdlib)                                │
│  missions · loops · recovery · guard/approvals · wire  │
│  spawn (Windows console / WSL) · delivery · accounts   │
│  UIA action runtime (structured, verified)             │
└───────┬──────────────────┬─────────────────────────────┘
        │                  │
   agent CLIs          Windows
   claude / codex      UIA tree · processes · win32
   (per-agent creds)   (no screenshots on the hot path)
```

## Roadmap (six weeks)

1. **Now** — reposition (this spec, README), approval queue surfaced in the
   mini bar island, UIA runtime slice with verified actions.
2. **Operational hygiene deepening** — per-agent credential scoping beyond
   accounts (scoped env, secret vault via DPAPI); WSL-side spawn parity;
   resumable-after-reboot audit trail viewer in the dashboard.
3. **UIA runtime maturing** — tree query language, action + assertion
   pairs agents can call as tools (MCP server exposing UIA), recorded
   traces for replay/debugging.
4. **Fifteen users** — ship a zip/installer, onboard people running
   Claude Code/Codex loops, iterate on what they scream about.
5. **UI pass** — glass design language (see design notes) applied to the
   control-plane surfaces: missions, approvals, audit, agents.

## Cut from the roadmap (parked, not deleted)

Consumer surface: calendar/life dashboard, email triage, daily-brief-as-
consumer-ritual, voice-first minibar, file concierge, watchers, form filler,
WhatsApp/Notes readers. The code that exists stays; nothing new gets built
here until the dev wedge has its fifteen users.

## Naming note

"Agent OS" framing is retired — OS signals undecided scope. It's **Wisp**:
a small, sharp tool that runs your agents. Repo rename
(`rune_agent_os` → `wisp`) worth doing while stars are near zero.

## Trust model (unchanged, now the headline)

- Engine binds 127.0.0.1 only; that is the security boundary.
- Destructive/outward/spending/credential actions require explicit approval,
  bound to a request id; automation can never self-approve.
- Every observable action lands on the append-only wire; if it's not on the
  wire, it didn't happen.
- Honest failure: classified errors, bounded retries, and stalled states are
  surfaced as stalled — never silently rewritten as success.

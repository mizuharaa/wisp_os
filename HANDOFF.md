# Maestro — agent handoff

Written 2026-07-08 for the next agent (or a cold-start session) picking up this repo.
Read `soul/soul.md` and `CLAUDE.md` first — this file is the operational map on top of them.

## What Maestro is

Claude Code as a personal AI operating system for **Daniel (Khang Daniel Tran)**. One
*conductor* that keeps memory fresh, reuses hard-won skills, spawns specialist agents only
when needed, and never re-solves a solved problem. It is a **substrate**: a future consumer
will run on top of it (Daniel's memory holds its name). Maestro must contain **zero
references to that consumer** — `bootstrap.py` has a hygiene check that fails if it appears anywhere,
including the event log. Naming history: **AIOS → EMBER → Maestro** (Daniel rejected EMBER;
run any new product name past him before committing to it).

Location: `C:\Users\user\OneDrive\Desktop\Python Env\agentic_os`. MIT. Python 3 stdlib only —
no dependencies, no build step.

## Status (as of commit 09b3f59)

Working and verified end-to-end: all 8 layers, the dashboard, SSH + local instance spawning,
the real-vault brain graph. `python bootstrap.py` → **12/12 checks pass**. Three commits:
`8d38c60` (AIOS v1), `1e71a44` (Maestro rename + Donezo rebuild), `09b3f59` (vault graph, SSH,
named instances, tree-shaped skills).

Skill registry goal is `stand up Maestro v1`. Earned: `automation`, `web-design`. Learning:
`3d-interaction`, `loop-engineering`, `skill-creation`, `workflow-audit`. Archived:
`vault-gardening` (decayed off-goal — intended, demonstrates the prune engine).

## Run + verify

```
python bootstrap.py          # 12-check verification — run this first, always
python bootstrap.py boot     # the CLAUDE.md boot sequence (soul, vault, skills, announce)
python desktop.py            # LOCAL app: chromeless Edge --app window; kills server on close
python dashboard/serve.py    # or just the server -> http://127.0.0.1:8817/dashboard/
```

**It is a local app, not a webapp** — Daniel has said this repeatedly. `desktop.py` is the
intended entry point (no browser chrome). The server binds `127.0.0.1` only; that IS the
security boundary — anyone who can POST to it can spawn permission-skipping agents. Never
bind `0.0.0.0`.

## Architecture (the 8 layers)

| Path | What it is |
|---|---|
| `soul/soul.md` | Identity, mission, beliefs. Hand-edited ONLY — the guard blocks automated writes. Drift log in `soul/CHANGELOG.md`. |
| `CLAUDE.md` | The map + boot sequence every session runs. |
| `.claude/hooks/guard.py` | PreToolUse gate. Blocks gated classes (destructive-delete, deploy, external-send, spend, soul-write) unless `state/approvals.json` holds a token. Exit 2 = block. `test_guard.py` is its self-check (7 cases). |
| `.claude/hooks/mirror.py` | The event wire. Writes every event to `state/events.jsonl`. Hook mode (stdin JSON) + manual mode (`--stage build --detail "..."`). Spawns emit at **PreToolUse** because the subagent tool is named `Agent`, not `Task`, in Claude Code 2.x. |
| `.claude/hooks/approve.py` | Mints time-boxed approval tokens for gated actions. |
| `skills/engine.py` | Earn/prune engine. Skills earn after **3 real uses**, decay after **2 prune strikes** when off-goal. Registry: `skills/registry.json`. |
| `.claude/agents/*.md` | 9 specialists (ceo, eng-manager, designer, reviewer, qa-lead, security-officer, release-engineer, doc-engineer, hermes). Spawn-on-demand via `/spawn`. |
| `.claude/commands/*.md` | Mission loop: `/office-hours → /plan-ceo-review → /plan-eng-review → build → /review → /qa → /ship`, plus `/spawn /goal /hermes /skill`. |
| `memory/pipeline.py` | Non-rot memory. No naked facts — every write carries a source + freshness stamp. `vault_path()` is the single source of truth for the Obsidian vault location (read from `memory/OBSIDIAN.md`). |
| `memory/harvest.py` | Workspace scraper: walks the parent workspace dir, mines each project's docs, writes sourced knowledge cards to the vault at `Maestro/Knowledge/` (+ `_index` MOC). Re-run to refresh. |
| `hermes/hermes.py` | The flywheel. `note`/`query`/`stale`. Notes append to `solved.jsonl` AND mirror a card into the vault at `Maestro/Hermes/` with an auto-regenerated `_index.md` MOC. |
| `.claude/hooks/recall.py` | UserPromptSubmit hook: queries Hermes with EVERY prompt and injects prior solutions into context — the relearn-before-resolve step is automatic now, in every session. |
| `dashboard/orchestrator.py` | The conductor loop: headless worker (`claude -p`, `--resume` between rounds) → haiku critic → accept/revise/reject verdicts, auto or gated on Daniel. State in `state/orchestrations/`, everything on the wire under the loop's oid. |
| `dashboard/askpass.py` | DPAPI ssh credential store (ctypes, this-Windows-account-only) + `SSH_ASKPASS` helper. Secrets live in gitignored `state/ssh-creds.json`; `askpass.cmd` is the shim ssh executes (`SSH_ASKPASS_REQUIRE=force`). |

**The wire (`state/events.jsonl`) is the one file everything observable flows through.** If it
isn't on the wire, it didn't happen. The dashboard reads only this + the registries + the vault.

## The dashboard (`dashboard/`)

Single-file vanilla HTML/CSS/JS (`index.html`) + stdlib server (`serve.py`). Left-navbar SPA:
Dashboard / Instances / Skill tree / Brain / Brain graph / Integrations / Audit log / Guard.

**Server endpoints** (all in `serve.py`):
- `GET /api/instances` — managed windows + liveness (checks PID).
- `GET /api/integrations` — MCPs, hooks, agents, and the slash-command list.
- `GET /api/vault` — walks the real Obsidian vault, returns notes + folders + wikilink edges (20s cache).
- `GET /api/vault-note?path=` — safe (path-checked) preview read of one note.
- `POST /api/spawn` — launch a terminal. Fields: `mission`, `name`, `mode` (tab/background),
  `model`, `budget`, `skip` (permission-skip toggle), `ssh` ({host,user,port,dir}).
  Tabs launch via `conhost.exe` so the window's PID == the tracked PID (used by focus).
- `POST /api/focus {sid}` / `POST /api/close {sid}` — Win32 foreground / taskkill by PID.
- `POST /api/message` — queue a directive to `state/inbox.jsonl` + wire.

**Instance manager** lets Daniel launch local OR SSH Claude Code terminals, name them, tick
model/budget/skip-permissions, and compose slash commands into the prompt. Focus pops the real
window. Since the orchestrator rebuild it also has: **Orchestrated mode** (the conductor loop
runs the mission hands-free — see orchestrator.py), **Quick launch** (one click, saved
defaults, standing default mission), an optional **DPAPI-encrypted ssh password store**
(typed-in-window remains the default), and **click-to-expand / copy buttons** on every
clamped mission or output (state survives the 2.5s re-render via the `OPEN` set).

**Every spawn exports `MAESTRO_SID=<sid>`** and mirror.py prefers it, so an instance's wire
events land under the id the dashboard tracks — instance cards show the live stage rail,
agent chips, and last action of their own session. This is the one mechanism that unifies
Terminals ↔ Session feed ↔ orchestrations; don't break it.

**Brain graph** renders the *actual vault* (folder-clustered, wikilinks as edges, drag +
click-to-read with an Obsidian deep link), with a toggle to the Hermes solved-problem view.

## Design constraints — READ BEFORE TOUCHING THE UI

Daniel notices slop fast and has corrected the design direction twice. The current spec lives
in his memory (`~/.claude/.../memory/design-taste.md`); the load-bearing rules:

- **Donezo/shadcn-neutral language:** flat neutral canvas, crisp WHITE cards clearly separated
  from it. **NEVER** wash the whole page in a tinted gradient — that reads as AI slop to him.
- **One brand color** (deep green `#135c38` + mint `#a9dcbd`), ink neutrals, red for
  destructive only. **Max 3 colors — the pie/donut is the only exemption.**
- **Recurring circular motifs** are the theme: capsule bars with diagonal-hatch empty state,
  270° radial gauge, circular corner arrow-buttons, one dark feature card per view.
- **Real icons only** (inline Lucide paths) — never hand-drawn scribble SVGs. Sentence case,
  never shouty all-caps microcopy. No fake status dots.
- **Graphs = organic force-directed monochrome** (Obsidian-like), not rigid orbital rings.
- **Local app, not webapp.** shadcn is the component bar (he runs its MCP; preset `b27GcrRo`
  = neutral zinc — a scaffold sits in the session scratchpad if a React migration is wanted).
- **ALWAYS verify at ~1280px** (his effective viewport at his display scaling), not just 1600.
  Every grid/flex child that holds text needs `min-width:0` or the layout blows out
  horizontally and "nothing shows up" (see Hermes `1a60c33`).

Verify UI changes with the Playwright MCP: navigate (cache-bust the URL with `?v=N`),
screenshot, read it back, check the console is clean. The server caches nothing but the
browser does — bump `?v=` after editing `index.html`.

## Known issues / caveats / deferred work

- **Vault graph caps at 240 notes** for render perf (`serve.py vault_tree`). Daniel's vault is
  ~81 now. If it grows past ~240, add folder-level filtering rather than rendering everything.
- **Session labels are still partly raw hex ids** in some lists. Mapping wire session ids to
  their mission text would make the feed read like a team roster. (Instances already show names.)
- **Windows-only** for instance management: PID liveness, conhost launch, and Win32 focus all
  use ctypes/taskkill. `focus_by()` uses the undocumented `SwitchToThisWindow` (only reliable
  foreground-from-background call) — upgrade to AttachThreadInput if it ever regresses.
- **SSH assumes Claude Code is installed on the remote host** and that `ssh` is on PATH.
  The remote command now recreates a login-shell PATH (profile/nvm/~/.local/bin) before
  running claude — `ssh host "cmd"` is a non-login shell (hermes `26bcd4e`).
- **Orchestrations don't survive a server restart** — the loop thread dies with the
  process; the run's JSON stays and the dashboard shows it as "stalled". Relaunch it.
- **askpass is a .cmd shim** — if Win32-OpenSSH ever refuses batch files as SSH_ASKPASS,
  ssh silently falls back to prompting in the window (graceful, but saved passwords idle).
- **`state/windows.json` accumulates stale entries** across restarts (dead PIDs show as Exited).
  It's reset to `{"windows": []}` during testing; harmless but not auto-pruned.
- The **conscious-spend** rule is a live convention: every spawn picks model + turn budget
  deliberately (haiku mechanical / sonnet workhorse / opus hard reasoning), not by reflex.

## Conventions

- Shell is Windows. **Don't hand-quote JSON with backslashes/em-dashes through bash** — it
  mangles and you get "bad json" (Hermes `b085a5e`, `1a60c33`). Build payloads in Python or curl carefully.
- Git args starting with `/` get MSYS-path-mangled in git bash (Hermes `086cf17`).
- End commits with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- After hard problems: `python hermes/hermes.py note "<problem>" "<solution>" --tags ... --source ...`.

## Context that lives outside the repo

Daniel's persistent memory (`C:\Users\user\.claude\projects\C--Users-user\memory\`): `agentic-os.md`
(this project), `design-taste.md` (the UI spec above), `conscious-agent-spend.md`, plus the index
`MEMORY.md`. His Obsidian vault is `C:\Obsidian_Brain\Daniel_Obsidian_Vault` — Maestro reads all
of it and writes only under `Maestro/`.

## Suggested first move for the next agent

Run `python bootstrap.py` (confirm 12/12), then `python desktop.py` and click through all 8
tabs to see live state before changing anything. If the task is UI, re-read `design-taste.md`
first and verify at 1280px.

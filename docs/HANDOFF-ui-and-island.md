# Handoff — dashboard UI pass + Dynamic Island

Current as of 2026-07-29. Repository: `C:\Users\user\OneDrive\Desktop\wisp`.
Scope: legacy dashboard theming/contrast, the recorded-history endpoints, the
parked React rebuild, and Phase 1 of the Dynamic Island.

For the CEO/delivery-pipeline background see `docs/HANDOFF.md` (2026-07-20) —
that work is untouched by this session.

**Nothing here is committed.** Everything below is in the working tree. Run
`git diff` before you change anything; `git status` lists 21 modified files
plus 6 new ones.

---

## 0. Read this first

**Rotate two credentials.** `state/pulse.json` was printed during this session
with insufficient redaction, exposing the GitHub PAT (`ghp_…`) and the Gmail
app password in a transcript. They are still live. This is outstanding.

**There are two dashboards and two islands.** Getting this wrong wastes hours
— it cost this session a full phase of work in the wrong file.

| Surface | File | Status |
|---|---|---|
| Dashboard (the real one) | `dashboard/index.html` (5.8k lines, vanilla) | **active** — this is what ships |
| Dashboard (React rebuild) | `dashboard/web/` (Vite + React 19 + Tailwind 4) | **parked** — built, then shelved by the operator |
| Island (the real one) | `app/minibar.html` — its own Electron window | **active** — Phase 1 done |
| Island (in-page) | `#island` inside `dashboard/index.html` | **hidden** via CSS, controller still runs |

The operator's instruction was explicit: work on the old dashboard, the
Electron island only. Do not "helpfully" revive the React app.

---

## 1. How it runs

The Electron app owns everything. `app/main.js`'s `ensureEngine()` spawns
`python dashboard/serve.py` as a child process and `will-quit` kills it. Start
the app and the engine follows; nothing depends on a browser or a terminal:

```
cd app && ./node_modules/electron/dist/electron.exe .
```

Verify the engine is really the app's child (not a stray from a previous run):

```powershell
Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
  Where-Object { $_.CommandLine -match 'serve\.py' } |
  Select-Object ProcessId, ParentProcessId
```

`ParentProcessId` must be the Electron main process. If it is not, a leftover
engine is squatting on port 8817 and the app quietly reused it — kill it,
restart the app.

Routes: `/dashboard/` → `dashboard/index.html` (pinned; the React build used to
shadow this URL whenever a `dist/` existed, which is why the operator kept
landing on a UI they had asked to stop working on). The React build lives at
`/dashboard/next.html`.

---

## 2. Done — legacy dashboard (`dashboard/index.html`)

### Dark mode now exists
It was dead, and not because of the toggle. The glass palette is an
unconditional `:root{}` block layered *after* `tokens.css`'s
`[data-theme="dark"]` rules, so flipping the attribute changed the base tokens
and then this block overwrote them every time. Added the missing dark half:
graphite room (`--void:#101013`), low-saturation nebula, panel fill
`rgba(255,255,255,0.10)`, blur 30px, edges 22%/28%, inks `#F4F4F7`/80%/58%,
accent lifted to `#8B7CFF`. The thin material (topbar, rail) had a hardcoded
`brightness(1.18)` that blows out a dark room — 1.06 in dark only.

### One status recipe replaced four
The same idea was drawn four ways: hardcoded light hexes, hardcoded *dark*
hexes from the pre-glass plum palette, token pairs, and solid saturated fills.
On graphite that produced brown slabs, neon pills and orange paragraphs. Now
four families (`--ok-*`, `--warn-*`, `--bad-*`, `--mut-*`), each a tinted
ground plus a legible ink of the same hue, mapped across `.pill`, `.rstat`
(all 12 states), `.deltapill`, `.tag`, `.delivery-blocked`, `.ai-review`,
`.brief-notice`, `.proof-boundary`. **Prose inside a state block stays
neutral** — only the label and marker wear the hue.

Dark grounds are 9–10%, not 14%: they stack on the panel's own 7% white over
graphite, and at 14% the amber one composited to an olive slab.

### `--canvas` was pointing at the room
Used 38 times as an *inset* surface (recall receipts, role flow, tracks, empty
states) but mapped to `--rune-paper` — the backdrop itself. Every recess
painted a slab of room inside a translucent panel: near-black in dark,
mid-grey in light, matching neither. Now a step away from its own panel:
`rgba(20,18,39,0.06)` light, `rgba(255,255,255,0.05)` dark. **This one fix
reaches every screen** — it was the Brain tab's "text doesn't match" bug.

### Contrast and type
Ink `#1A1830 → #141227`, secondary `0.62 → 0.80`, muted `0.40 → 0.60`, panel
fill `0.60 → 0.68`, chart hairlines `0.16 → 0.22`. Body weight 450, headings
640–680.

### Bugs fixed along the way
- **`$("n-skill")` was null** — the skill tree was folded into Brain but
  `render()` still wrote to its badge, throwing every 2.5s poll and aborting
  everything after that line. Half the header was dead because of it.
- **The Run-it split button** — §5.3's "one button geometry" re-rounded
  `.cmdgo` to 10px on all four corners while `.cmddrop` kept `0 13px 13px 0`:
  two mismatched shapes with a seam. The wrapper owns the radius now; the
  halves are flat segments split by a hairline.
- **Storage health** inherited `white-space:nowrap`, running 302px past a
  289px column with nothing to scroll — half of every ledger line unreadable.
- **The account chips were white-on-white** (measured 1.15:1). A
  `html[data-theme="dark"] .ccard` block re-scoped those cards to light inks,
  written when account cards were solid white brand cards. Under the glass
  material they are translucent panels like everything else. Deleted; the
  chip now measures 5.24:1 from rendered pixels.
- **The plum brand was still driving every chart** (`GREEN="#5c1346"` etc. in
  JS) — a fifth hue on a page that runs on iris. Walked onto the accent, along
  with both contribution-heatmap ramps and the capacity hatch. No plum hexes
  remain in the file.
- Eleven `TODO` scaffold cards removed from the overview.

### The nav rail
Left exactly as it was. Three different designs were tried this session
(floating capsule with hover-morph, portfolio-style scroll-contract, vertical
scroll-morph) and the operator rejected all of them. The original full-height
sticky sidebar with `pointerenter → railopen` is restored **verbatim** — `git
diff` on the nav is empty. Do not redesign it again without being asked.

---

## 3. Done — recorded history (`dashboard/pulse.py`, `serve.py`)

Pulse only ever knew "now": every refresh overwrote the last snapshot, so
nothing could draw usage over time. Two recorders were added, both independent
of any UI:

- **`state/usage.jsonl`** — appended from the existing 45s pulse loop,
  `{ts, claude, codex}` where each value is that connector's 5h utilisation
  (Claude reports per account; the band is the account nearest its ceiling).
  Throttled to one row per 4 min, trimmed to a rolling 48h.
  Served by `GET /api/usage-series?hours=N` (clamped 1–48).
- **`state/activity-grid.json`** — an incremental scan of the Claude and Codex
  transcript JSONLs into absolute hour buckets, with a per-file byte cursor so
  only appended bytes are read. First pass reads the ~81MB 7-day tail (a few
  seconds, in the pulse thread); every pass after is near-free. Served by
  `GET /api/activity-grid` as `grid[weekday][hour]`, local time, 7 days.

Both are gitignored. `python dashboard/pulse.py --selfcheck` covers the
line→hour extraction and the bucket→grid fold; it passes.

Real numbers at time of writing: ~16k transcript events across the last 7 days,
usage series filling since the recorder went in.

---

## 4. Parked — the React rebuild (`dashboard/web/`)

Built to a spec (dark violet glass, floating rail, real widgets), then the
operator said to stop and go back to the old dashboard. It is **not deleted**
because it is uncommitted and unrecoverable if removed. It builds clean
(`npm run build`), typechecks, and is reachable at `/dashboard/next.html`.

If you are told to scrap it: `dashboard/web/src/{components/{glass,viz,top-nav}.tsx,
features/overview/,pages/overview-page.tsx,lib/overview.ts}` plus the `dist/`.
The two backend recorders are independent and must stay.

Two findings from it worth keeping:

- **Lightning CSS (Tailwind v4's transformer) silently kills
  `backdrop-filter`.** It emitted only `-webkit-backdrop-filter`, which current
  Chromium no longer supports — every glass panel shipped with zero blur and
  nothing warned. `browserslist` and Vite's `cssTarget` both failed to change
  it. Fix: hold the value in a CSS variable (`backdrop-filter: var(--bf-mid)`)
  so the transformer cannot pattern-match it.
- Taking a `position:fixed` sidebar out of flow drops the grid's content into
  the rail track. Pin `.main { grid-column: 2 }`.

---

## 5. Done — Dynamic Island, Phase 1 of 4

Spec: `app/minibar.html` is the island. It is a frameless, transparent,
always-on-top Electron window; the pill inside it morphs and the window follows.

**Material** (measured): `rgba(18,14,34,0.72)`, `blur(40px) saturate(180%)`,
border `rgba(255,255,255,0.14)`, `inset 0 1px 0 rgba(255,255,255,0.18)` +
`0 20px 50px -14px rgba(0,0,0,0.70)`. Dark glass in both themes — it is a
separate object from the dashboard, not a piece of it.

**Eight states, all measured exact:**

| state | w × h | radius |
|---|---|---|
| dormant | 200 × 34 | 999 |
| listening | 420 × 48 | 999 |
| thinking | 460 × 52 | 999 |
| working | 520 × 64 | 26 |
| live | 320 × 38 | 999 |
| peek | 560 × auto (≤320) | 24 |
| toast | 340 × 52 | 18 |
| minimized | 56 × 28 | 999 |

**The morph.** One element, one spring (`k380 c30 m0.9`), width/height/radius
all lerped off a single `t` — never two elements swapping. Content ducks out
via a `.morphing` class and fades back at 120ms while the container is still
settling; that lag is the whole illusion. `window.islandState(name)` drives it
from a console. The in-page island additionally has a `?dev=island` switcher.

**The window follows the island** via `minibar:bounds` IPC — resized once per
transition (never per frame), to the larger of (from, to) so nothing clips
mid-morph, centred on its own centre so it grows symmetrically. `MARGIN = 18`
in `minibar.html` must stay in sync with `#island`'s CSS margin.

**Drag and click are renderer-driven.** `-webkit-app-region: drag` was removed:
an OS drag region never delivers a click, which is why clicking the island did
nothing. The renderer now moves the window itself on pointer drag
(`minibar:move`) and treats anything under 4px of travel as a click. Do not
reintroduce `app-region: drag` on the pill.

**Do not reintroduce `setIgnoreMouseEvents`.** It was added so the transparent
margin would not eat clicks behind the window; it also swallows the mousedown
that dragging needs, and if the pointer-enter that was meant to switch it off
never fires, the island becomes permanently inert. The window hugs the island
instead.

**Ctrl+Shift+Space** swaps surfaces: dashboard away + island up, press again
for the dashboard back. The original `isFocused()` test was dropped — it made
the shortcut silently do nothing whenever anything else held focus, including
the island itself. The island no longer auto-shows on minimise/close.

**Panel readability + markdown.** The panel's surfaces were still hardcoded
from the old light theme while `--ink` had gone near-white — white text on
white cards. All re-themed. Replies were written in with `textContent`, so
`**bold**` and `-` bullets showed their syntax; there is now a small markdown
renderer (bold, italic, inline + fenced code, lists, headings, links) that
HTML-escapes first, since that is untrusted model output going into
`innerHTML`.

---

## 6. Not done — Island Phases 2–4

**Phase 2 — motion and minimize.** Running border exists as a static gradient
ring (currently `opacity:0` at rest, on when busy) but is *not* the conic
`--angle` sweep with per-activity colours and speeds. No drag elasticity,
momentum, rubber-banding, or magnetic snap zones. No merge/fan for multiple
live widgets. `minimized` exists as a size only — it does not travel to a
screen edge, grow a badge, or peek on hover.

**Phase 3 — status voice + widget registry.** Not started. The status line
should be coloured verb + mono object with a 900ms dwell and left-truncation;
the literal/humorous register rule (humour only when no tool is running) is
implemented in the *in-page* island's controller and could be lifted. The
registry (`IslandWidget`, `LiveSource`, compact/expanded renderers, visual-first
rule) does not exist — widget kinds are still implicit.

**Phase 4 — routing and browser control.** Not started, and this is the one the
operator actually wants. Today `send()` posts to `/api/chat`, a plain chat
answer with no tools bound — which is why asking it to find a car got "I don't
have real-time internet access". Needs: the `browse` state with a live step
stream, the page thumbnail with the acted-on element ringed, audit-log entries
per action, and a **confirm gate on anything that spends money, sends a
message, or deletes** — non-negotiable per the spec.

The repo already has `dashboard/browser.py` (Chrome DevTools Protocol) and
`dashboard/uia.py` (Windows UI Automation) exposed at `/api/browser/*` and
`/api/uia/*`. Phase 4 should drive those, not invent a new mechanism.

---

## 7. Traps that cost time this session

1. **rAF is throttled to ~1fps when the window is occluded.** Spring
   animations stall mid-flight and every measurement is wrong. Screenshots do
   not fix it. For measurement only, patch
   `window.requestAnimationFrame = cb => setTimeout(() => cb(performance.now()), 16)`,
   settle, then restore. Never ship that.
2. **`dashboard/index.html` is 5.8k lines with heavy specificity collisions.**
   Two real bugs came from a later group rule outranking a base rule:
   `#island[data-state="peek"]` is listed in the `.glass-thick` group, which
   set `position:relative` (the island fell out of the viewport to y=2001) and
   the light panel material (it turned white mid-morph). Always check what else
   claims a selector before adding CSS.
3. Unlayered CSS beats Tailwind's layered utilities. A `.glass{position:relative}`
   silently killed a `fixed` utility class.
4. The Playwright browser and the app are unrelated; the operator's "as soon
   as I close the browser it's dead" was because the engine had been started
   from an agent session instead of by the app. Let the app own it.

---

## 8. Open decisions for the operator

- Should the React rebuild be deleted or kept parked?
- Should the in-page island be deleted outright rather than CSS-hidden? Its
  controller still drives `body[data-agent]`, which the mesh heartbeat uses, so
  removing the element needs that wire re-homed.
- Island start-up: currently hidden until Ctrl+Shift+Space. Should it be
  visible at launch instead?
- Phase order: 2 → 3 → 4 as specced, or jump to 4 (browser control) since that
  is the capability being asked for?

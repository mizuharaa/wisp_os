/** Every derivation the overview grid needs, in one place, so the widgets stay
 * presentational and it is obvious which numbers are real.
 *
 * What the backend genuinely exposes (see the build report):
 * Everything here comes from a real source: Claude quota, Codex plan/credits,
 * missions, wire events, skills, brain cards + vault graph, GitHub series,
 * recorded usage series, 7x24 activity grid. No placeholder data remains. */
import type {
  BrainPayload,
  CeoRun,
  OrchLoop,
  Pulse,
  SkillRegistry,
  UsageSeries,
  VaultTree,
  WireEvent,
} from "@/lib/api"

export type Status = "running" | "done" | "queued" | "blocked"

export const STATUS_COLOR: Record<Status, string> = {
  running: "var(--amber)",
  done: "var(--mint)",
  queued: "var(--slate)",
  blocked: "var(--coral)",
}

const RUNNING = new Set(["running", "retrying", "repairing", "planning", "review"])
const BLOCKED = new Set(["waiting_permission", "exhausted", "failed", "error", "blocked"])
const QUEUED = new Set(["queued", "pending", "idle"])

function statusOf(raw: string): Status {
  const s = (raw || "").toLowerCase()
  if (RUNNING.has(s)) return "running"
  if (BLOCKED.has(s)) return "blocked"
  if (QUEUED.has(s)) return "queued"
  return "done"
}

// ---------------------------------------------------------------- formatting
export function elapsed(fromIso?: string, toIso?: string): string {
  if (!fromIso) return "--"
  const a = Date.parse(fromIso)
  const b = toIso ? Date.parse(toIso) : Date.now()
  if (!Number.isFinite(a) || !Number.isFinite(b)) return "--"
  return short(Math.max(0, (b - a) / 1000))
}

export function short(secs: number): string {
  if (secs < 60) return `${Math.round(secs)}s`
  if (secs < 3600) return `${Math.floor(secs / 60)}m`
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`
  return `${Math.floor(secs / 86400)}d`
}

export function countdown(epoch?: number | null): string {
  if (!epoch) return "--"
  const left = epoch * 1000 - Date.now()
  if (left <= 0) return "resets now"
  const h = Math.floor(left / 3600000)
  const m = Math.floor((left % 3600000) / 60000)
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`
}

export function clock(iso?: string): string {
  const t = iso ? Date.parse(iso) : NaN
  if (!Number.isFinite(t)) return "--:--"
  const d = new Date(t)
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`
}

// ---------------------------------------------------------------- connectors
export interface Connector {
  id: string
  name: string
  handle: string
  pct: number | null
  resetAt: number | null
  note: string
  face: [string, string]
  mock: boolean
  /** recorded 12h utilisation for the card back, null until 2+ samples. */
  series: number[] | null
  sessions: number | null
  lastSync: string
}

export function deriveConnectors(pulse: Pulse | null, usage: UsageSeries | null): Connector[] {
  const series = (key: string) => {
    const values = (usage?.rows ?? []).map((r) => Number(r[key]) || 0)
    return values.length > 1 ? values : null
  }
  const accounts = pulse?.claude?.accounts ?? []
  const usable = accounts.filter((a) => typeof a.pct === "number")
  // the account nearest its ceiling is the one that actually gates work
  const lead = usable.length
    ? usable.reduce((a, b) => ((a.pct ?? 0) >= (b.pct ?? 0) ? a : b))
    : accounts[0]
  const codex = pulse?.codex

  return [
    {
      id: "claude",
      name: "Claude CLI",
      handle: lead?.email || lead?.name || "not signed in",
      pct: typeof lead?.pct === "number" ? lead.pct : null,
      resetAt: lead?.reset_at ?? null,
      note: lead?.error
        ? lead.error
        : `${accounts.length} account${accounts.length === 1 ? "" : "s"} · 7d ${lead?.pct7d ?? "--"}%`,
      face: ["#6d5bf0", "#3a2f8f"],
      mock: false,
      series: series("claude"),
      sessions: accounts.length || null,
      lastSync: pulse?.asof ? `${short(Date.now() / 1000 - pulse.asof)} ago` : "--",
    },
    {
      id: "codex",
      name: "Codex",
      handle: codex?.email || "not connected",
      pct: typeof codex?.pct === "number" ? codex.pct : null,
      resetAt: codex?.reset_at ?? null,
      note: codex?.error
        ? codex.error
        : `${codex?.plan ?? "--"} · credits ${codex?.credits ?? "--"}`,
      face: ["#1f9d92", "#37506b"],
      mock: false,
      series: series("codex"),
      sessions: null,
      lastSync: codex?.asof ? `${short(Date.now() / 1000 - codex.asof)} ago` : "--",
    },
    // Only connectors this repo actually talks to. A Figma card existed here as
    // a placeholder; nothing in the codebase reads Figma, so it is gone.
  ]
}

// ------------------------------------------------------------------ missions
export interface Mission {
  id: string
  title: string
  target: string
  status: Status
  progress: number
  elapsed: string
  startedAt: number
}

export function deriveMissions(
  ceoRuns: CeoRun[],
  ceoHistory: CeoRun[],
  orchestrations: OrchLoop[],
): Mission[] {
  const fromCeo = [...ceoRuns, ...ceoHistory].map((run) => {
    const roles = run.roles ?? []
    const done = roles.filter((r) => statusOf(r.status) === "done").length
    return {
      id: run.cid,
      title: run.name || (run.goal as string) || "mission",
      // no target-file field exists on a run; workdir is the nearest real thing
      target: (run.workdir as string) || (run.account as string) || "—",
      status: statusOf(run.status),
      progress: roles.length ? done / roles.length : statusOf(run.status) === "done" ? 1 : 0,
      elapsed: elapsed(run.started as string, run.updated as string),
      startedAt: Date.parse((run.started as string) || (run.updated as string) || "") || 0,
    }
  })
  const fromOrch = orchestrations.map((loop) => ({
    id: loop.oid,
    title: loop.name || "loop",
    target: (loop.dir as string) || "—",
    status: statusOf(loop.status),
    progress: Number(loop.rounds) ? Number(loop.round) / Number(loop.rounds) : 0,
    elapsed: elapsed(loop.started as string, loop.updated as string),
    startedAt: Date.parse((loop.started as string) || (loop.updated as string) || "") || 0,
  }))
  const rank: Record<Status, number> = { running: 0, blocked: 1, queued: 2, done: 3 }
  return [...fromCeo, ...fromOrch].sort(
    (a, b) => rank[a.status] - rank[b.status] || b.startedAt - a.startedAt,
  )
}

// --------------------------------------------------------------- stat tiles
export interface Stat {
  label: string
  value: number
  delta: number
  series: number[]
  color: string
}

/** Bucket ISO timestamps into the last `days` daily counts (oldest first). */
function daily(stamps: number[], days = 7): number[] {
  const out = new Array(days).fill(0)
  const start = new Date()
  start.setHours(0, 0, 0, 0)
  const day0 = start.getTime() - (days - 1) * 86400000
  for (const t of stamps) {
    const i = Math.floor((t - day0) / 86400000)
    if (i >= 0 && i < days) out[i] += 1
  }
  return out
}

const parse = (v?: string) => Date.parse(v || "")

export function deriveStats(
  ceoRuns: CeoRun[],
  ceoHistory: CeoRun[],
  orchestrations: OrchLoop[],
  wireEvents: WireEvent[],
  brain: BrainPayload | null,
  pulse: Pulse | null,
): Stat[] {
  const missionStamps = [...ceoRuns, ...ceoHistory, ...orchestrations]
    .map((r) => parse((r.started as string) || (r.updated as string)))
    .filter(Number.isFinite)
  const wireStamps = wireEvents.map((e) => parse(e.ts)).filter(Number.isFinite)
  const hitStamps = (brain?.receipts ?? [])
    .filter((r) => r.outcome === "hit")
    .map((r) => parse(r.ts))
    .filter(Number.isFinite)
  const commits = (pulse?.github?.days ?? []).slice(-7).map((d) => d.count)

  const tile = (label: string, series: number[], color: string): Stat => {
    const half = Math.floor(series.length / 2) || 1
    const recent = series.slice(-half).reduce((a, b) => a + b, 0)
    const prior = series.slice(-2 * half, -half).reduce((a, b) => a + b, 0)
    return {
      label,
      value: series.reduce((a, b) => a + b, 0),
      delta: recent - prior,
      series,
      color,
    }
  }

  return [
    tile("missions 7d", daily(missionStamps), "var(--iris)"),
    tile("wire events 7d", daily(wireStamps), "var(--cyan)"),
    tile("recall hits 7d", daily(hitStamps), "var(--mint)"),
    tile("commits 7d", commits.length ? commits : new Array(7).fill(0), "var(--amber)"),
  ]
}

// ------------------------------------------------------------ recent actions
export interface ActionRow {
  ts: string
  agent: string
  action: string
  detail: string
  status: Status
}

export function deriveActions(wireEvents: WireEvent[], ceoHistory: CeoRun[]): ActionRow[] {
  const wire: ActionRow[] = wireEvents.map((e) => ({
    ts: e.ts || "",
    agent: e.session || "system",
    action: e.event || "event",
    detail: e.detail || "",
    status: statusOf(String(e.event || "").includes("fail") ? "failed" : "done"),
  }))
  // role attempts carry the only per-action status the repo records
  const attempts: ActionRow[] = ceoHistory.flatMap((run) =>
    (run.roles ?? []).flatMap((role) =>
      ((role.attempts as { ts?: string; kind?: string; status?: string; model?: string }[]) ?? []).map(
        (a) => ({
          ts: a.ts || "",
          agent: `${run.cid}/${role.id}`,
          action: a.kind || "attempt",
          detail: `${a.model ?? "?"} · ${role.title}`,
          status: statusOf(a.status || ""),
        }),
      ),
    ),
  )
  return [...wire, ...attempts]
    .filter((r) => r.ts)
    .sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts))
}

// ------------------------------------------------------------ skill coverage
export interface SkillBar {
  name: string
  status: string
  uses: number
  color: string
}

const SKILL_COLOR: Record<string, string> = {
  active: "var(--mint)",
  learning: "var(--amber)",
  candidate: "var(--cyan)",
  archived: "var(--slate)",
}

export function deriveSkills(registry: SkillRegistry) {
  const rows = Object.entries(registry.skills ?? {})
  const bars: SkillBar[] = rows
    .map(([name, s]) => ({
      name,
      status: s.status,
      uses: s.uses ?? 0,
      color: SKILL_COLOR[s.status] ?? "var(--slate)",
    }))
    .sort((a, b) => b.uses - a.uses)
  const active = bars.filter((b) => b.status === "active").length
  return { bars, active, total: bars.length }
}

// ----------------------------------------------------------------- brain
export function deriveBrain(brain: BrainPayload | null, vault: VaultTree | null) {
  const cards = brain?.storage?.cards?.active_count ?? 0
  const stale = brain?.storage?.cards?.stale_count ?? 0
  const notes = vault?.notes?.length ?? 0
  const freshDays = 14
  const cut = Date.now() / 1000 - freshDays * 86400
  const fresh = (vault?.notes ?? []).filter((n) => n.mtime >= cut).length
  return {
    cards,
    notes,
    stale,
    fresh,
    edges: vault?.links?.length ?? 0,
    hits: brain?.summary?.hits ?? 0,
    attempts: brain?.summary?.attempts ?? 0,
  }
}

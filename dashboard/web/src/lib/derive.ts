import type { WireEvent } from "@/lib/api"

const LIVE_MS = 15 * 60 * 1000

export interface WireSession {
  id: string
  agents: { name: string; detail: string }[]
  exits: number
  last: string | null
  lastline: string
  job: string
  live: boolean
}

/** Port of dashboard/index.html's sessions(): groups the raw wire-event log
 * by session id, deriving each session's live/idle state, last activity,
 * and the launched agents it saw exit.
 * → skipped: the stage/change-count badges the legacy card also shows
 * (STAGES, CHG). Add if the ported card needs them; the dismiss fix here
 * doesn't. */
export function deriveSessions(events: WireEvent[]): WireSession[] {
  const byId = new Map<string, WireSession>()
  for (const e of events) {
    const id = String(e.session || "?")
    if (id === "operator") continue
    if (!byId.has(id)) {
      byId.set(id, { id, agents: [], exits: 0, last: null, lastline: "", job: "" , live: false })
    }
    const s = byId.get(id)!
    s.last = String(e.ts || s.last || "") || null
    if (e.event === "spawn") s.agents.push({ name: String(e.agent || "agent"), detail: String(e.detail || "") })
    if (e.event === "agent-exit") s.exits++
    if (e.detail) {
      s.lastline = String(e.detail)
      if (!s.job && !/^(session online|conductor idle)/i.test(String(e.detail))) s.job = String(e.detail)
    }
  }
  const out = [...byId.values()]
  for (const s of out) s.live = !!s.last && Date.now() - Date.parse(s.last) < LIVE_MS
  out.sort((a, b) => Number(b.live) - Number(a.live) || (b.last || "").localeCompare(a.last || ""))
  return out
}

/** Port of dashboard/index.html's doneIds(): a directive is "done" once a
 * directive-done wire event's detail text mentions its 8-hex-char id. */
export function deriveDoneIds(events: WireEvent[]): Set<string> {
  const ids = new Set<string>()
  for (const e of events) {
    if (e.event !== "directive-done") continue
    for (const m of String(e.detail || "").matchAll(/[0-9a-f]{8}/g)) ids.add(m[0])
  }
  return ids
}

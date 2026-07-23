import type { CeoRole, CeoRun } from "@/lib/api"

export interface Exchange {
  cid: string
  prompt: string
  route: "answer" | "solo" | "delegate" | ""
  status: string
  reply: string
  roles: CeoRole[]
  live: boolean
  cost: number
  ts: string
}

const ROLE_STATUS_LABEL: Record<string, string> = {
  done: "done",
  working: "working",
  running: "working",
  review: "in review",
  waiting_permission: "waiting for approval",
  retrying: "retrying",
  repairing: "repairing",
  failed: "failed",
  blocked: "blocked",
  exhausted: "exhausted",
  stopped: "stopped",
  pending: "queued",
}

function roleSummary(role: CeoRole): string {
  const label = ROLE_STATUS_LABEL[role.status] ?? role.status
  return `${role.title || role.id} — ${label}`
}

function summarizeRoles(roles: CeoRole[]): string {
  const done = roles.filter((r) => r.status === "done").length
  const waiting = roles.find((r) => r.status === "waiting_permission")
  const failed = roles.find((r) => ["failed", "blocked"].includes(r.status))
  let line = `${done}/${roles.length} role${roles.length === 1 ? "" : "s"} done`
  if (waiting) line += ` — ${waiting.title || waiting.id} waiting for approval`
  else if (failed) line += ` — ${failed.title || failed.id} ${failed.status}`
  return line
}

function toExchange(run: CeoRun): Exchange {
  const prompt = run.goal || run.name || ""
  const roles = run.roles ?? []
  let reply = ""
  if (run.route === "answer") {
    reply = run.reply || ""
  } else if (roles.length === 1) {
    reply = roles[0].status === "done" ? roles[0].result || "" : roleSummary(roles[0])
  } else {
    reply = roles.length ? summarizeRoles(roles) : "The CEO is staffing the roles…"
  }
  return {
    cid: run.cid,
    prompt,
    route: (run.route as Exchange["route"]) || "",
    status: run.status,
    reply,
    roles,
    live: !!run.live,
    cost: Number(run.cost) || 0,
    ts: run.finished_at || run.updated || run.started || "",
  }
}

/** Merges active + completed CEO runs into one chronological (oldest-first)
 * exchange list. A run can briefly appear in both lists right after
 * finishing (list_history()'s own comment: "the API cannot briefly lose a
 * completion between its final save and the worker thread's archive
 * cleanup") -- runs wins on conflict since it's live-accurate. */
export function deriveExchanges(runs: CeoRun[], history: CeoRun[]): Exchange[] {
  const byCid = new Map<string, CeoRun>()
  for (const r of history) byCid.set(r.cid, r)
  for (const r of runs) byCid.set(r.cid, r)
  return [...byCid.values()].map(toExchange).sort((a, b) => a.ts.localeCompare(b.ts))
}

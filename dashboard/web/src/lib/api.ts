/** Typed fetch wrappers for the endpoints Phase 1 uses. Mirrors
 * dashboard/index.html's jget()/jsonl() -- non-.jsonl endpoints throw on a
 * non-OK response; .jsonl endpoints tolerate a missing file (empty list). */

export interface InstanceWindow {
  sid: string
  name?: string
  alive?: boolean
  [key: string]: unknown
}

export interface OrchLoop {
  oid: string
  name: string
  status: string
  live?: boolean
  dismissed?: boolean
  [key: string]: unknown
}

export interface WireEvent {
  session?: string
  event?: string
  ts?: string
  detail?: string
  [key: string]: unknown
}

export interface DirectiveEntry {
  // "done" status is derived from wire events (see lib/derive.ts's
  // deriveDoneIds), not a field on the directive record itself.
  id?: string
  text?: string
  ts?: string
  [key: string]: unknown
}

export interface CeoRole {
  id: string
  title: string
  status: string
  model?: string
  turns?: number
  cost?: number
  secs?: number
  result?: string
  detail?: string
  depends_on?: string[]
  review?: boolean
  permission_request?: { [key: string]: unknown }
  [key: string]: unknown
}

export interface CeoRun {
  cid: string
  name?: string
  goal?: string
  route?: "answer" | "solo" | "delegate"
  status: string
  roles: CeoRole[]
  reply?: string // answer route only
  cost?: number
  started?: string
  finished_at?: string
  updated?: string
  live?: boolean
  archived?: boolean
  dismissed?: boolean
  [key: string]: unknown
}

export interface CeoOpts {
  mode?: "auto" | "answer" | "solo" | "delegate"
  refine?: "auto" | "off"
  model?: "auto" | "haiku" | "sonnet" | "opus" | "fable"
  effort?: "auto" | "quick" | "standard" | "deep"
  account?: string
  gate?: boolean
}

export interface SkillEntry {
  status: "active" | "learning" | "candidate" | "archived"
  uses: number
  desc: string
  branch: string
  trigger: string
  goals: string[]
  decay: number
  created: string
  last_used: string | null
}

export interface SkillRegistry {
  goal: string
  updated: string
  skills: Record<string, SkillEntry>
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path, { cache: "no-store" })
  if (!res.ok) throw new Error(`${path}: ${res.status}`)
  return res.json() as Promise<T>
}

async function getJsonl<T>(path: string): Promise<T[]> {
  const res = await fetch(path, { cache: "no-store" })
  if (!res.ok) return []
  const text = await res.text()
  return text
    .split("\n")
    .filter((line) => line.trim())
    .map((line) => {
      try {
        return JSON.parse(line) as T
      } catch {
        return null
      }
    })
    .filter((v): v is T => v !== null)
}

export async function fetchInstances(): Promise<InstanceWindow[]> {
  const doc = await getJson<{ windows?: InstanceWindow[] }>("/api/instances")
  return doc.windows ?? []
}

export async function fetchOrchestrations(): Promise<OrchLoop[]> {
  const doc = await getJson<{ orchestrations?: OrchLoop[] }>("/api/orchestrations")
  return doc.orchestrations ?? []
}

export function fetchWireEvents(): Promise<WireEvent[]> {
  return getJsonl<WireEvent>("/state/events.jsonl")
}

export function fetchDirectives(): Promise<DirectiveEntry[]> {
  return getJsonl<DirectiveEntry>("/state/inbox.jsonl")
}

export async function fetchVersion(): Promise<{ v: number; boot: number } | null> {
  try {
    return await getJson("/api/version")
  } catch {
    return null
  }
}

export function fetchCeo(): Promise<{ runs: CeoRun[]; history: CeoRun[] }> {
  return getJson("/api/ceo")
}

export interface CeoMessageResult {
  kind: "answer" | "mission"
  cid?: string
  reply?: string
  name?: string
  route?: string
  error?: string
}

export async function postCeoMessage(
  text: string,
  opts: CeoOpts = {},
): Promise<CeoMessageResult> {
  const res = await fetch("/api/ceo", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, opts }),
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(body.error || `ceo failed: ${res.status}`)
  return body
}

export function fetchSkillRegistry(): Promise<SkillRegistry> {
  return getJson("/skills/registry.json")
}

export async function postAddSkill(name: string, branch: string, trigger: string): Promise<void> {
  const res = await fetch("/api/skill", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, branch, trigger }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error || `skill add failed: ${res.status}`)
  }
}

export async function postOrchAction(
  oid: string,
  action: string,
  feedback = "",
): Promise<void> {
  const res = await fetch("/api/orch-action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ oid, action, feedback }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error || `orch-action failed: ${res.status}`)
  }
}

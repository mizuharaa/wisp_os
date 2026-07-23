/** The 9 routes the legacy dashboard defines (dashboard/index.html's
 * ROUTE_ORDER). Only "instances" has ported content in Phase 1 — the rest
 * render a stub linking back to the classic dashboard. */
export type RouteId =
  | "overview"
  | "calendar"
  | "instances"
  | "skills"
  | "brain"
  | "graph"
  | "integrations"
  | "audit"
  | "guard"

export const ROUTE_ORDER: readonly RouteId[] = [
  "overview",
  "calendar",
  "instances",
  "skills",
  "brain",
  "graph",
  "integrations",
  "audit",
  "guard",
]

export const ROUTE_LABELS: Record<RouteId, string> = {
  overview: "Dashboard",
  calendar: "Calendar",
  instances: "Agent console",
  skills: "Skill tree",
  brain: "Brain",
  graph: "Brain graph",
  integrations: "Integrations",
  audit: "Audit log",
  guard: "Guard",
}

export const PORTED_ROUTES: ReadonlySet<RouteId> = new Set(["instances", "skills"])

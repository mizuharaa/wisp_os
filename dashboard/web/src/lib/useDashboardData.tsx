import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react"
import {
  fetchCeo,
  fetchDirectives,
  fetchInstances,
  fetchOrchestrations,
  fetchSkillRegistry,
  fetchVersion,
  fetchWireEvents,
  type CeoRun,
  type DirectiveEntry,
  type InstanceWindow,
  type OrchLoop,
  type SkillRegistry,
  type WireEvent,
} from "@/lib/api"

const POLL_MS = 2500

const EMPTY_SKILL_REGISTRY: SkillRegistry = { goal: "", updated: "", skills: {} }

export interface DashboardData {
  instances: InstanceWindow[]
  orchestrations: OrchLoop[]
  wireEvents: WireEvent[]
  directives: DirectiveEntry[]
  ceoRuns: CeoRun[]
  ceoHistory: CeoRun[]
  skillRegistry: SkillRegistry
  /** false once a poll cycle fails -- mirrors the legacy app's S.wire flag. */
  wire: boolean
  /** Re-run the poll immediately (mutation-then-refetch pattern). */
  refetch: () => Promise<void>
}

const DashboardDataContext = createContext<DashboardData | null>(null)

export function DashboardDataProvider({ children }: { children: React.ReactNode }) {
  const [instances, setInstances] = useState<InstanceWindow[]>([])
  const [orchestrations, setOrchestrations] = useState<OrchLoop[]>([])
  const [wireEvents, setWireEvents] = useState<WireEvent[]>([])
  const [directives, setDirectives] = useState<DirectiveEntry[]>([])
  const [ceoRuns, setCeoRuns] = useState<CeoRun[]>([])
  const [ceoHistory, setCeoHistory] = useState<CeoRun[]>([])
  const [skillRegistry, setSkillRegistry] = useState<SkillRegistry>(EMPTY_SKILL_REGISTRY)
  const [wire, setWire] = useState(true)
  const lastVersion = useRef<number | null>(null)

  const poll = useCallback(async () => {
    try {
      const [inst, orch, events, dirs, ceo, skills, version] = await Promise.all([
        fetchInstances(),
        fetchOrchestrations(),
        fetchWireEvents(),
        fetchDirectives(),
        fetchCeo(),
        fetchSkillRegistry(),
        fetchVersion(),
      ])
      setInstances(inst)
      setOrchestrations(orch)
      setWireEvents(events)
      setDirectives(dirs)
      setCeoRuns(ceo.runs)
      setCeoHistory(ceo.history)
      setSkillRegistry(skills)
      setWire(true)
      if (version) {
        if (lastVersion.current === null) lastVersion.current = version.v
        // A later phase can surface a "dashboard updated, reload" banner here
        // the way dashboard/index.html's checkVersion() does -- not needed
        // until more of the app is actually served from the new build.
      }
    } catch {
      setWire(false)
    }
  }, [])

  useEffect(() => {
    poll()
    const id = setInterval(poll, POLL_MS)
    return () => clearInterval(id)
  }, [poll])

  const value: DashboardData = {
    instances,
    orchestrations,
    wireEvents,
    directives,
    ceoRuns,
    ceoHistory,
    skillRegistry,
    wire,
    refetch: poll,
  }

  return (
    <DashboardDataContext.Provider value={value}>{children}</DashboardDataContext.Provider>
  )
}

export function useDashboardData(): DashboardData {
  const ctx = useContext(DashboardDataContext)
  if (!ctx) throw new Error("useDashboardData must be used within DashboardDataProvider")
  return ctx
}

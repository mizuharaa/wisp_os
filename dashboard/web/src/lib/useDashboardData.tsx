import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react"
import {
  fetchActivityGrid,
  fetchBrain,
  fetchCeo,
  fetchDirectives,
  fetchInstances,
  fetchOrchestrations,
  fetchPulse,
  fetchSkillRegistry,
  fetchUsageSeries,
  fetchVault,
  fetchVersion,
  fetchWireEvents,
  type ActivityGrid,
  type BrainPayload,
  type CeoRun,
  type DirectiveEntry,
  type InstanceWindow,
  type OrchLoop,
  type Pulse,
  type SkillRegistry,
  type UsageSeries,
  type VaultTree,
  type WireEvent,
} from "@/lib/api"

const POLL_MS = 2500
/** pulse refreshes server-side every 45s and the vault/grid scans are the
 * expensive reads -- polling them on the 2.5s wire cadence is pure waste. */
const SLOW_EVERY = 12

const EMPTY_SKILL_REGISTRY: SkillRegistry = { goal: "", updated: "", skills: {} }

export interface DashboardData {
  instances: InstanceWindow[]
  orchestrations: OrchLoop[]
  wireEvents: WireEvent[]
  directives: DirectiveEntry[]
  ceoRuns: CeoRun[]
  ceoHistory: CeoRun[]
  skillRegistry: SkillRegistry
  pulse: Pulse | null
  brain: BrainPayload | null
  vault: VaultTree | null
  usage: UsageSeries | null
  activity: ActivityGrid | null
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
  const [pulse, setPulse] = useState<Pulse | null>(null)
  const [brain, setBrain] = useState<BrainPayload | null>(null)
  const [vault, setVault] = useState<VaultTree | null>(null)
  const [usage, setUsage] = useState<UsageSeries | null>(null)
  const [activity, setActivity] = useState<ActivityGrid | null>(null)
  const [wire, setWire] = useState(true)
  const lastVersion = useRef<number | null>(null)
  const tick = useRef(0)

  const poll = useCallback(async () => {
    const slow = tick.current++ % SLOW_EVERY === 0
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
      if (version && lastVersion.current === null) lastVersion.current = version.v
    } catch {
      setWire(false)
    }
    if (!slow) return
    // Each of these degrades on its own: a missing vault must not blank the
    // connector cards, and vice versa.
    void fetchPulse().then(setPulse).catch(() => {})
    void fetchBrain().then(setBrain).catch(() => {})
    void fetchVault().then(setVault).catch(() => {})
    void fetchUsageSeries(12).then(setUsage).catch(() => {})
    void fetchActivityGrid().then(setActivity).catch(() => {})
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
    pulse,
    brain,
    vault,
    usage,
    activity,
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

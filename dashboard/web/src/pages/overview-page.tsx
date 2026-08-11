import { ConnectorHero } from "@/features/overview/connector-hero"
import {
  BrainTile,
  LiveActivity,
  MissionStack,
  RecentActions,
  SkillCoverage,
  StatTile,
  UsageBurndown,
} from "@/features/overview/widgets"
import { useDashboardData } from "@/lib/useDashboardData"
import {
  deriveActions,
  deriveBrain,
  deriveConnectors,
  deriveMissions,
  deriveSkills,
  deriveStats,
} from "@/lib/overview"

/** 12 columns, 22px gutters. Every panel owns a hard height (see widgets). */
function Cell({
  span,
  delay,
  children,
}: {
  span: number
  delay: number
  children: React.ReactNode
}) {
  return (
    <div
      className="w-mount"
      style={{ gridColumn: `span ${span}`, ["--d" as string]: `${delay * 28}ms` }}
    >
      {children}
    </div>
  )
}

export function OverviewPage() {
  const d = useDashboardData()
  const connectors = deriveConnectors(d.pulse, d.usage)
  const missions = deriveMissions(d.ceoRuns, d.ceoHistory, d.orchestrations)
  const stats = deriveStats(
    d.ceoRuns,
    d.ceoHistory,
    d.orchestrations,
    d.wireEvents,
    d.brain,
    d.pulse,
  )
  const actions = deriveActions(d.wireEvents, d.ceoHistory)
  const skills = deriveSkills(d.skillRegistry)
  const brain = deriveBrain(d.brain, d.vault)

  return (
    <div className="grid grid-cols-12 gap-[22px] p-7">
      <Cell span={7} delay={0}>
        <ConnectorHero connectors={connectors} />
      </Cell>
      <Cell span={5} delay={1}>
        <MissionStack missions={missions} />
      </Cell>

      {stats.map((s, i) => (
        <Cell key={s.label} span={3} delay={2 + i}>
          <StatTile stat={s} />
        </Cell>
      ))}

      <Cell span={7} delay={6}>
        <UsageBurndown usage={d.usage} />
      </Cell>
      <Cell span={5} delay={7}>
        <LiveActivity activity={d.activity} />
      </Cell>

      <Cell span={8} delay={8}>
        <RecentActions rows={actions} />
      </Cell>
      <Cell span={4} delay={9}>
        <SkillCoverage bars={skills.bars} active={skills.active} total={skills.total} />
      </Cell>

      <Cell span={12} delay={10}>
        <BrainTile brain={brain} edges={(d.vault?.links ?? []) as [number, number][]} />
      </Cell>
    </div>
  )
}

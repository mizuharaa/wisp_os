import { useState } from "react"
import { Card } from "@/components/ui/card"
import { AddSkillForm } from "@/features/skills/add-skill-form"
import { SkillDetailPanel } from "@/features/skills/skill-detail-panel"
import { SkillGraph } from "@/features/skills/skill-graph"
import type { SkillGraphNode } from "@/lib/graphLayout"
import { useDashboardData } from "@/lib/useDashboardData"

export function SkillsPage() {
  const { skillRegistry } = useDashboardData()
  const [selected, setSelected] = useState<SkillGraphNode | null>(null)

  const selectSkillByName = (name: string) => {
    const entry = skillRegistry.skills[name]
    if (!entry) return
    setSelected({
      key: "skill:" + name,
      type: "skill",
      label: name,
      x: 0,
      y: 0,
      vx: 0,
      vy: 0,
      r: 4,
      color: "",
      branch: entry.branch,
      skill: entry,
      phase: 0,
    })
  }

  return (
    <div className="flex flex-col gap-6 p-6">
      <div>
        <h1 className="font-heading text-xl font-semibold">Skill tree</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Skills earn after 3 real uses and decay when off-goal. Click a node for details.
        </p>
      </div>
      <div className="flex flex-col gap-4 lg:flex-row">
        <Card className="min-h-[560px] flex-1 overflow-hidden p-2">
          <SkillGraph registry={skillRegistry} selectedKey={selected?.key ?? null} onSelect={setSelected} />
        </Card>
        <Card className="min-w-0 p-4 lg:w-80">
          <SkillDetailPanel selected={selected} registry={skillRegistry} onSelectSkill={selectSkillByName} />
        </Card>
      </div>
      <Card className="p-4">
        <AddSkillForm />
      </Card>
    </div>
  )
}

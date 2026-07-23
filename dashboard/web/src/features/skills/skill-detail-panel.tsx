import { Badge } from "@/components/ui/badge"
import type { SkillRegistry } from "@/lib/api"
import type { SkillGraphNode } from "@/lib/graphLayout"

const STATUS_WORD: Record<string, string> = {
  active: "earned",
  learning: "learning",
  candidate: "new",
  archived: "archived",
}
const STATUS_TONE: Record<string, string> = {
  active: "bg-success text-white",
  learning: "bg-info text-white",
  candidate: "bg-secondary text-secondary-foreground",
  archived: "bg-secondary text-secondary-foreground",
}

function shortDesc(desc: string): string {
  const first2 = desc.trim().split(/(?<=[.!?])\s/).slice(0, 2).join(" ")
  return first2.length > 220 ? first2.slice(0, 217) + "…" : first2
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })
  } catch {
    return iso
  }
}

export function SkillDetailPanel({
  selected,
  registry,
  onSelectSkill,
}: {
  selected: SkillGraphNode | null
  registry: SkillRegistry
  onSelectSkill: (name: string) => void
}) {
  if (!selected) {
    return (
      <p className="text-sm text-muted-foreground">
        Click a node for its full description. Filled ring = earned; the ring shows progress
        toward 3 uses.
      </p>
    )
  }

  if (selected.type === "root") {
    const entries = Object.values(registry.skills)
    const earned = entries.filter((v) => v.status === "active").length
    const inProgress = entries.filter((v) => v.status === "learning" || v.status === "candidate").length
    const xp = entries.reduce((a, v) => a + (v.uses || 0), 0)
    const pct = entries.length ? Math.round((earned / entries.length) * 100) : 0
    return (
      <div className="flex flex-col gap-3">
        <h3 className="font-heading text-sm font-semibold">Rune</h3>
        <div className="grid grid-cols-2 gap-3 text-sm">
          <div>
            <div className="text-xs text-muted-foreground">Skills earned</div>
            <div className="font-medium">
              {earned}/{entries.length} <span className="text-xs text-success">▲ {pct}%</span>
            </div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">In progress</div>
            <div className="font-medium">{inProgress}</div>
          </div>
          <div className="col-span-2">
            <div className="text-xs text-muted-foreground">Conductor XP</div>
            <div className="font-medium">{xp} total uses</div>
          </div>
        </div>
        <Badge variant="outline" className="w-fit">
          goal: {registry.goal || "–"}
        </Badge>
      </div>
    )
  }

  if (selected.type === "branch") {
    if (selected.locked) {
      return (
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <h3 className="font-heading text-sm font-semibold capitalize">{selected.label}</h3>
            <Badge variant="outline">locked</Badge>
          </div>
          <p className="text-sm text-muted-foreground">
            Opens when a mission earns a skill here — or seed one with the panel below.
          </p>
        </div>
      )
    }
    const items = Object.entries(registry.skills).filter(([, v]) => (v.branch || "misc") === selected.label)
    const earned = items.filter(([, v]) => v.status === "active").length
    return (
      <div className="flex flex-col gap-3">
        <div className="flex items-center gap-2">
          <h3 className="font-heading text-sm font-semibold capitalize">{selected.label}</h3>
          <Badge className="bg-success text-white">
            {earned}/{items.length} earned
          </Badge>
        </div>
        <div className="flex flex-col gap-1.5">
          {items.map(([name, v]) => (
            <button
              key={name}
              onClick={() => onSelectSkill(name)}
              className="flex items-center justify-between gap-2 rounded-md border border-border p-2 text-left text-sm hover:bg-muted"
            >
              <span className="min-w-0 truncate">{name}</span>
              <Badge className={STATUS_TONE[v.status]}>{STATUS_WORD[v.status]}</Badge>
            </button>
          ))}
        </div>
      </div>
    )
  }

  // skill leaf
  const v = selected.skill!
  const uses = Math.min(v.uses || 0, 3)
  const onGoal = (v.goals && v.goals.length) ? `working toward: ${v.goals.join(", ")}` : "not yet attached to a goal"
  const desc = (v.desc || "").trim()
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <h3 className="font-heading text-sm font-semibold">{selected.label}</h3>
        <Badge className={STATUS_TONE[v.status]}>{STATUS_WORD[v.status]}</Badge>
      </div>
      {desc ? (
        <p className="text-sm">{shortDesc(desc)}</p>
      ) : (
        <p className="text-sm text-muted-foreground">
          Added {formatDate(v.created)} · {onGoal}.
        </p>
      )}
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <span>{uses}/3 uses</span>
        <span>{v.trigger || "no trigger"}</span>
        <span className="capitalize">{v.branch || "misc"}</span>
        <span>{v.goals && v.goals.length ? "on goal" : "off goal"}</span>
      </div>
    </div>
  )
}

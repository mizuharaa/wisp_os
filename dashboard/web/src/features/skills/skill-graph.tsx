import { useEffect, useMemo, useReducer, useRef } from "react"
import { Boxes, FileCode, Lock, Palette, Radar, Wrench } from "lucide-react"
import type { SkillRegistry } from "@/lib/api"
import { createSkillGraphSim, isSettled, type SkillGraphNode } from "@/lib/graphLayout"
import { useReducedMotion } from "@/lib/useReducedMotion"

const VIEW = { w: 900, h: 700 }

const BRANCH_ICONS: Record<string, React.ComponentType<{ size?: number }>> = {
  engineering: Wrench,
  design: Palette,
  meta: FileCode,
  ops: Radar,
}

function iconFor(node: SkillGraphNode) {
  if (node.type === "branch") {
    if (node.locked) return Lock
    return BRANCH_ICONS[node.label] ?? Boxes
  }
  return null
}

export function SkillGraph({
  registry,
  selectedKey,
  onSelect,
}: {
  registry: SkillRegistry
  selectedKey: string | null
  onSelect: (node: SkillGraphNode) => void
}) {
  // Rebuilds only when the registry's actual content changes (not on every
  // 2.5s poll tick where nothing changed) -- mirrors the reference's own
  // skSig early-return pattern (dashboard/index.html's renderSkills()).
  // The sim's internal node positions mutate in place every animation
  // frame via sim.step(); this object identity staying stable across
  // unrelated re-renders is exactly what makes that safe.
  const sig = JSON.stringify(registry)
  const sim = useMemo(() => createSkillGraphSim(registry, VIEW), [sig])

  const hoverRef = useRef<string | null>(null)
  const reduced = useReducedMotion()
  const [, rerender] = useReducer((n: number) => n + 1, 0)

  useEffect(() => {
    if (reduced) {
      // Matches the reference's own reduced-motion handling: settle
      // synchronously (no visible animation) instead of skipping the
      // simulation entirely -- reduced-motion users still get the real
      // settled layout, just without the transition.
      let speed = 0
      let iterations = 0
      do {
        speed = sim.step(1, false)
        iterations++
      } while (!isSettled(sim.alpha, speed) && iterations < 600)
      rerender()
      return
    }
    let rafId = 0
    let lastTime = 0
    const loop = (now: number) => {
      if (document.hidden) {
        rafId = requestAnimationFrame(loop)
        return
      }
      if (!lastTime) lastTime = now
      const dt = Math.min(3, (now - lastTime) / (1000 / 60))
      lastTime = now
      const hovered = !!hoverRef.current
      const speed = sim.step(dt, hovered)
      rerender()
      if (!isSettled(sim.alpha, speed) || hovered) {
        rafId = requestAnimationFrame(loop)
      }
    }
    rafId = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(rafId)
  }, [sim, reduced])

  return (
    <svg
      viewBox={`0 0 ${VIEW.w} ${VIEW.h}`}
      preserveAspectRatio="xMidYMid meet"
      className="h-full w-full"
      role="img"
      aria-label="Skill tree graph"
    >
      <g>
        {sim.links.map((link, i) => {
          const a = sim.nodes[link.a]
          const b = sim.nodes[link.b]
          return (
            <line
              key={i}
              x1={a.x}
              y1={a.y}
              x2={b.x}
              y2={b.y}
              stroke="var(--rune-rule)"
              strokeWidth={1}
              vectorEffect="non-scaling-stroke"
            />
          )
        })}
      </g>
      <g>
        {sim.nodes.map((node) => {
          const Icon = iconFor(node)
          const selected = node.key === selectedKey
          const ring = node.type === "skill" && node.skill ? Math.min(node.skill.uses, 3) / 3 : null
          return (
            <g
              key={node.key}
              transform={`translate(${node.x} ${node.y})`}
              onClick={() => onSelect(node)}
              onMouseEnter={() => (hoverRef.current = node.key)}
              onMouseLeave={() => (hoverRef.current = null)}
              role="button"
              tabIndex={0}
              aria-label={node.label}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault()
                  onSelect(node)
                }
              }}
              style={{ cursor: "pointer", opacity: node.locked ? 0.55 : 1 }}
            >
              {ring !== null && (
                <circle
                  r={node.r + 3}
                  fill="none"
                  stroke="var(--rune-ink2)"
                  strokeWidth={1.5}
                  strokeDasharray={`${2 * Math.PI * (node.r + 3) * ring} ${2 * Math.PI * (node.r + 3)}`}
                  strokeLinecap="round"
                  transform="rotate(-90)"
                  opacity={0.7}
                />
              )}
              <circle
                r={node.r}
                fill={node.color}
                stroke={selected ? "var(--rune-focus)" : "none"}
                strokeWidth={selected ? 2 : 0}
              />
              {Icon && (
                <g transform="translate(-7 -7)" color="var(--rune-on-accent)">
                  <Icon size={14} />
                </g>
              )}
              <text
                y={node.r + 13}
                textAnchor="middle"
                fontSize={node.type === "root" ? 12 : 10}
                fontWeight={node.type === "root" ? 700 : 500}
                fill="var(--rune-ink)"
              >
                {node.label}
              </text>
            </g>
          )
        })}
      </g>
    </svg>
  )
}

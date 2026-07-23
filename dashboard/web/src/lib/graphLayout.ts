import type { SkillEntry, SkillRegistry } from "@/lib/api"

/** Ports, at skill-tree scale (root -> ~7 branch hubs -> <=11 skill leaves,
 * no drag/pan/zoom needed), the confirmed-real force simulation from
 * dashboard/index.html's Brain graph (hash() at line 3845, physicsStep()/
 * loop() at 3991-4066): pairwise repulsion, spring links, weak center
 * gravity, velocity damping, alpha decay that self-stops the animation --
 * NOT a perpetual ambient wobble. The "idle = slow drift" from the design
 * memory IS this settle process; once alpha/speed drop below threshold the
 * simulation goes fully static, exactly like the reference. */

export const LOCKED_GENRES = ["integration", "voice", "vision"] as const

const GOLDEN = Math.PI * (3 - Math.sqrt(5))
const ALPHA_STOP = 0.014
const SPEED_STOP = 0.035
const ALPHA_DECAY = 0.982
const DAMPING_BASE = 0.84

export type NodeType = "root" | "branch" | "skill"

export interface SkillGraphNode {
  key: string
  type: NodeType
  label: string
  x: number
  y: number
  vx: number
  vy: number
  r: number
  color: string
  locked?: boolean
  branch?: string // skill nodes: their parent branch key
  skill?: SkillEntry // skill nodes only
  phase: number // hash-seeded, for future use (e.g. per-node stagger)
}

export interface SkillGraphLink {
  a: number // index into nodes
  b: number
}

/** Small FNV-1a-ish string hash, ported byte-for-byte from index.html:3845. */
export function hash(s: string): number {
  let h = 2166136261
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return h >>> 0
}

const BRANCH_COLORS = [
  "oklch(35.5% 0.115 330)", // matches --rune-accent's own hue
  "oklch(40% 0.1 300)",
  "oklch(42% 0.09 270)",
  "oklch(45% 0.08 200)",
]
const branchColorCache = new Map<string, string>()
function colorForBranch(branch: string): string {
  if (!branchColorCache.has(branch)) {
    branchColorCache.set(branch, BRANCH_COLORS[branchColorCache.size % BRANCH_COLORS.length])
  }
  return branchColorCache.get(branch)!
}

/** Same branch ordering dashboard/index.html's renderSkills() already uses:
 * earned-count desc, then size desc, then alpha -- reused, not reinvented. */
function orderedBranches(registry: SkillRegistry): [string, [string, SkillEntry][]][] {
  const byBranch = new Map<string, [string, SkillEntry][]>()
  for (const [name, entry] of Object.entries(registry.skills)) {
    const key = entry.branch || "misc"
    if (!byBranch.has(key)) byBranch.set(key, [])
    byBranch.get(key)!.push([name, entry])
  }
  return [...byBranch.entries()].sort((a, b) => {
    const activeA = a[1].filter(([, v]) => v.status === "active").length
    const activeB = b[1].filter(([, v]) => v.status === "active").length
    return activeB - activeA || b[1].length - a[1].length || a[0].localeCompare(b[0])
  })
}

export interface SkillGraphSim {
  nodes: SkillGraphNode[]
  links: SkillGraphLink[]
  degree: number[]
  alpha: number
  /** Advances the simulation one tick. Returns the max node speed this
   * step, mirroring the reference's self-stop condition (caller checks
   * alpha<=ALPHA_STOP && speed<=SPEED_STOP to know when to stop the rAF loop). */
  step(dt: number, hovered: boolean): number
}

/** Builds the initial (golden-angle/even-angle jittered) node/link layout
 * and returns a steppable simulation object. Root->branch placement uses
 * even angular spacing with a radius stagger (matches index.html's
 * folder-hub placement); branch->skill placement uses golden-angle spacing
 * with sqrt radius growth (matches index.html's note-around-folder
 * placement) -- these are two genuinely different formulas in the source,
 * ported as such rather than unified. */
export function createSkillGraphSim(registry: SkillRegistry, size: { w: number; h: number }): SkillGraphSim {
  const nodes: SkillGraphNode[] = []
  const links: SkillGraphLink[] = []
  const cx = size.w / 2
  const cy = size.h / 2

  const makeNode = (def: Omit<SkillGraphNode, "vx" | "vy" | "phase" | "x" | "y">, x: number, y: number): number => {
    nodes.push({ ...def, x, y, vx: 0, vy: 0, phase: (hash(def.key) % 6283) / 1000 })
    return nodes.length - 1
  }

  const rootIx = makeNode(
    { key: "root", type: "root", label: "Rune", r: 14, color: "var(--rune-ink)" },
    cx,
    cy,
  )

  const branches = orderedBranches(registry)
  const branchSlots: { key: string; items: [string, SkillEntry][]; locked: boolean }[] = [
    ...branches.map(([key, items]) => ({ key, items, locked: false })),
    ...LOCKED_GENRES.filter((g) => !branches.some(([key]) => key === g)).map((g) => ({
      key: g,
      items: [] as [string, SkillEntry][],
      locked: true,
    })),
  ]

  const branchCount = branchSlots.length
  branchSlots.forEach((slot, i) => {
    const angle = -Math.PI / 2 + (i / Math.max(1, branchCount)) * Math.PI * 2
    const stagger = 0.82 + (i % 3) * 0.11
    const radius = Math.min(size.w, size.h) * 0.32 * stagger
    const x = cx + radius * Math.cos(angle)
    const y = cy + radius * Math.sin(angle)
    const branchIx = makeNode(
      {
        key: "branch:" + slot.key,
        type: "branch",
        label: slot.key,
        r: slot.locked ? 7 : 9,
        color: slot.locked ? "var(--rune-muted)" : colorForBranch(slot.key),
        locked: slot.locked,
      },
      x,
      y,
    )
    links.push({ a: rootIx, b: branchIx })

    slot.items.forEach(([name, entry], j) => {
      const a = j * GOLDEN + (hash(name) % 1000) / 1000
      const r = 58 + Math.sqrt(j) * 31
      const sx = x + r * Math.cos(a)
      const sy = y + r * Math.sin(a)
      const uses = Math.min(entry.uses || 0, 3)
      const skillIx = makeNode(
        {
          key: "skill:" + name,
          type: "skill",
          label: name,
          r: 4 + uses * 0.4,
          color: colorForBranch(slot.key),
          branch: slot.key,
          skill: entry,
        },
        sx,
        sy,
      )
      links.push({ a: branchIx, b: skillIx })
    })
  })

  const degree = new Array(nodes.length).fill(0)
  for (const link of links) {
    degree[link.a]++
    degree[link.b]++
  }

  const sim: SkillGraphSim = {
    nodes,
    links,
    degree,
    alpha: 1,
    step(dt: number, hovered: boolean): number {
      const count = nodes.length
      if (!count) return 0
      const fx = new Float64Array(count)
      const fy = new Float64Array(count)
      const alpha = sim.alpha

      for (let i = 0; i < count; i++) {
        const a = nodes[i]
        for (let j = i + 1; j < count; j++) {
          const b = nodes[j]
          let dx = b.x - a.x
          let dy = b.y - a.y
          let d2 = dx * dx + dy * dy
          if (d2 > 129600) continue
          if (d2 < 0.01) {
            const angle = (hash(a.key + "|" + b.key) % 6283) / 1000
            dx = Math.cos(angle) * 0.1
            dy = Math.sin(angle) * 0.1
            d2 = 0.01
          }
          const d = Math.sqrt(d2)
          const ux = dx / d
          const uy = dy / d
          const isHub = a.type !== "skill" || b.type !== "skill"
          const gap = Math.max(a.r + b.r + 15, isHub ? 34 : 24)
          const repulse = Math.min(0.34, 260 / (d2 + 220)) * alpha
          const collision = d < gap ? Math.min(0.62, (gap - d) * 0.045) * Math.max(0.18, alpha) : 0
          const force = repulse + collision
          fx[i] -= ux * force
          fy[i] -= uy * force
          fx[j] += ux * force
          fy[j] += uy * force
        }
      }

      for (const link of links) {
        const a = nodes[link.a]
        const b = nodes[link.b]
        const dx = b.x - a.x
        const dy = b.y - a.y
        const d = Math.max(0.001, Math.sqrt(dx * dx + dy * dy))
        const norm = 1 / Math.sqrt(Math.max(1, degree[link.a], degree[link.b]))
        const rest = 112
        const strength = 0.014 * norm
        const force = Math.max(-0.28, Math.min(0.28, (d - rest) * strength)) * alpha
        const ux = dx / d
        const uy = dy / d
        fx[link.a] += ux * force
        fy[link.a] += uy * force
        fx[link.b] -= ux * force
        fy[link.b] -= uy * force
      }

      nodes.forEach((n, i) => {
        const gravity = n.type === "root" || n.type === "branch" ? 0.00013 : 0.000035
        fx[i] += (cx - n.x) * gravity * alpha
        fy[i] += (cy - n.y) * gravity * alpha
      })

      const damping = Math.pow(DAMPING_BASE, dt)
      const motion = hovered ? 0.2 : 1
      let maxSpeed = 0
      nodes.forEach((n, i) => {
        let ax = fx[i]
        let ay = fy[i]
        const acc = Math.sqrt(ax * ax + ay * ay)
        if (acc > 0.58) {
          ax *= 0.58 / acc
          ay *= 0.58 / acc
        }
        n.vx = (n.vx + ax * dt * motion) * damping
        n.vy = (n.vy + ay * dt * motion) * damping
        let speed = Math.sqrt(n.vx * n.vx + n.vy * n.vy)
        if (speed > 4.2) {
          n.vx *= 4.2 / speed
          n.vy *= 4.2 / speed
          speed = 4.2
        }
        n.x += n.vx * dt * motion
        n.y += n.vy * dt * motion
        const pad = 40
        if (n.x < pad) {
          n.x = pad
          if (n.vx < 0) n.vx = 0
        } else if (n.x > size.w - pad) {
          n.x = size.w - pad
          if (n.vx > 0) n.vx = 0
        }
        if (n.y < pad) {
          n.y = pad
          if (n.vy < 0) n.vy = 0
        } else if (n.y > size.h - pad) {
          n.y = size.h - pad
          if (n.vy > 0) n.vy = 0
        }
        maxSpeed = Math.max(maxSpeed, speed)
      })

      sim.alpha *= Math.pow(ALPHA_DECAY, dt)
      return maxSpeed
    },
  }
  return sim
}

export function isSettled(alpha: number, speed: number): boolean {
  return alpha <= ALPHA_STOP && speed <= SPEED_STOP
}

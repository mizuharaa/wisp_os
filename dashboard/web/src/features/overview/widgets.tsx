import { useState } from "react"
import { Chip, Glass, PanelHead } from "@/components/glass"
import { Constellation, Sparkline, StackedArea } from "@/components/viz"
import type { ActivityGrid, UsageSeries } from "@/lib/api"
import {
  clock,
  STATUS_COLOR,
  type ActionRow,
  type Mission,
  type SkillBar,
  type Stat,
} from "@/lib/overview"

const MONO = { fontFamily: "var(--w-mono)" } as const
const DISPLAY = { fontFamily: "var(--w-display)", letterSpacing: "-0.02em" } as const

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="num flex h-full items-center px-5 pb-5 text-[11px]" style={{ ...MONO, color: "var(--ink-3)" }}>
      {children}
    </div>
  )
}

// ------------------------------------------------------------ mission stack
export function MissionStack({ missions }: { missions: Mission[] }) {
  return (
    <Glass tier="mid" hover className="flex h-[340px] flex-col overflow-hidden">
      <PanelHead
        title="Missions"
        right={
          <Chip color="var(--ink-2)">
            {missions.filter((m) => m.status === "running").length} running
          </Chip>
        }
      />
      {missions.length === 0 ? (
        <Empty>no missions on the wire</Empty>
      ) : (
        <div className="flex-1 overflow-y-auto px-3 pb-3">
          {missions.map((m) => (
            <div key={m.id} className="flex h-[46px] items-center gap-3 rounded-[10px] px-2 hover:bg-white/5">
              <span
                className="size-2 shrink-0 rounded-full"
                style={{ background: STATUS_COLOR[m.status] }}
              />
              <div className="min-w-0 flex-1">
                <div className="truncate text-[15px] leading-tight">{m.title}</div>
                <div className="num truncate text-[11px]" style={{ ...MONO, color: "var(--ink-3)" }}>
                  {m.target}
                </div>
              </div>
              <div className="h-[2px] w-16 shrink-0 rounded-full bg-white/10">
                <div
                  className="h-full rounded-full"
                  style={{
                    width: `${Math.round(Math.min(1, Math.max(0, m.progress)) * 100)}%`,
                    background: STATUS_COLOR[m.status],
                  }}
                />
              </div>
              <span className="num w-12 shrink-0 text-right text-[11px]" style={{ ...MONO, color: "var(--ink-2)" }}>
                {m.elapsed}
              </span>
            </div>
          ))}
        </div>
      )}
    </Glass>
  )
}

// ---------------------------------------------------------------- stat tile
export function StatTile({ stat }: { stat: Stat }) {
  const sign = stat.delta > 0 ? "+" : ""
  const color = stat.delta > 0 ? "var(--mint)" : stat.delta < 0 ? "var(--coral)" : "var(--slate)"
  // light tier without its own blur: 8 blurring surfaces is the ceiling, and
  // the seven mid panels plus the nav rail already spend it.
  return (
    <Glass tier="light" hover className="flex h-[108px] flex-col justify-between p-4">
      <span className="num text-[11px]" style={{ ...MONO, color: "var(--ink-3)" }}>
        {stat.label}
      </span>
      <div className="flex items-end justify-between">
        <div className="flex items-baseline gap-2">
          <span className="num text-[32px] leading-none" style={DISPLAY}>
            {stat.value}
          </span>
          <Chip color={color}>
            {sign}
            {stat.delta}
          </Chip>
        </div>
        <Sparkline values={stat.series} color={stat.color} />
      </div>
    </Glass>
  )
}

// ----------------------------------------------------------- usage burndown
const BAND_COLOR: Record<string, string> = {
  claude: "var(--iris)",
  codex: "var(--cyan)",
}

export function UsageBurndown({ usage }: { usage: UsageSeries | null }) {
  const rows = usage?.rows ?? []
  const keys = usage?.keys ?? []
  const series = keys.map((k) => ({
    key: k,
    color: BAND_COLOR[k] ?? "var(--slate)",
    values: rows.map((r) => Number(r[k]) || 0),
  }))
  const labels = rows.length
    ? [rows[0], rows[Math.floor(rows.length / 2)], rows[rows.length - 1]].map((r) =>
        clock(new Date(r.ts * 1000).toISOString()),
      )
    : []

  return (
    <Glass tier="mid" hover className="flex h-[220px] flex-col overflow-hidden">
      <PanelHead
        title="Usage burndown · 12h"
        right={
          <div className="flex gap-1.5">
            {keys.length ? (
              keys.map((k) => (
                <Chip key={k} color={BAND_COLOR[k] ?? "var(--slate)"}>
                  {k}
                </Chip>
              ))
            ) : (
              <Chip color="var(--amber)">recording</Chip>
            )}
          </div>
        }
      />
      {rows.length < 2 ? (
        <Empty>
          usage history now recorded to state/usage.jsonl — the first samples land within
          minutes
        </Empty>
      ) : (
        <div className="flex-1 px-5 pb-4">
          <StackedArea series={series} labels={labels} height={128} />
        </div>
      )}
    </Glass>
  )
}

// ------------------------------------------------------------ live activity
const DAYS = ["M", "T", "W", "T", "F", "S", "S"]

export function LiveActivity({ activity }: { activity: ActivityGrid | null }) {
  const grid = activity?.grid ?? []
  const max = Math.max(1, ...grid.flat())
  return (
    <Glass tier="mid" hover className="flex h-[220px] flex-col overflow-hidden">
      <PanelHead
        title="Live activity · 7d"
        right={<Chip color="var(--ink-2)">{activity?.total ?? 0} events</Chip>}
      />
      {!grid.length ? (
        <Empty>scanning transcripts…</Empty>
      ) : (
        <div className="flex-1 overflow-hidden px-5 pb-4">
          <div className="flex flex-col gap-[3px]">
            {grid.map((row, d) => (
              <div key={d} className="flex items-center gap-[3px]">
                <span className="num w-3 text-[10px]" style={{ ...MONO, color: "var(--ink-3)" }}>
                  {DAYS[d]}
                </span>
                {row.map((v, h) => (
                  <span
                    key={h}
                    title={`${DAYS[d]} ${String(h).padStart(2, "0")}:00 · ${v}`}
                    className="heat-cell"
                    style={{
                      background: `color-mix(in srgb, var(--iris) ${
                        v ? 8 + Math.round((v / max) * 92) : 4
                      }%, transparent)`,
                    }}
                  />
                ))}
              </div>
            ))}
            <div className="flex gap-[3px] pl-3">
              {Array.from({ length: 24 }, (_, h) => (
                <span key={h} className="num w-[10px] text-center text-[10px]" style={{ ...MONO, color: "var(--ink-3)" }}>
                  {h % 6 === 0 ? h : ""}
                </span>
              ))}
            </div>
          </div>
        </div>
      )}
    </Glass>
  )
}

// ------------------------------------------------------------ recent actions
export function RecentActions({ rows }: { rows: ActionRow[] }) {
  return (
    <Glass tier="mid" hover className="flex h-[260px] flex-col overflow-hidden">
      <PanelHead title="Recent actions" right={<Chip color="var(--ink-2)">{rows.length}</Chip>} />
      {rows.length === 0 ? (
        <Empty>the wire is quiet</Empty>
      ) : (
        <div className="flex-1 overflow-y-auto px-5 pb-4">
          <table className="w-full table-fixed border-collapse text-[13px]">
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} className="h-9 border-t border-white/[0.07] first:border-0 hover:bg-white/5">
                  <td className="num w-14 text-[11px]" style={{ ...MONO, color: "var(--ink-2)" }}>
                    {clock(r.ts)}
                  </td>
                  <td className="w-28 truncate pr-2">{r.agent}</td>
                  <td className="w-24 truncate pr-2" style={{ color: "var(--ink-2)" }}>
                    {r.action}
                  </td>
                  <td className="num truncate pr-2 text-[11px]" style={{ ...MONO, color: "var(--ink-3)" }}>
                    {r.detail}
                  </td>
                  <td className="w-[86px] text-right">
                    <Chip color={STATUS_COLOR[r.status]}>{r.status}</Chip>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Glass>
  )
}

// ------------------------------------------------------------ skill coverage
export function SkillCoverage({
  bars,
  active,
  total,
}: {
  bars: SkillBar[]
  active: number
  total: number
}) {
  const max = Math.max(1, ...bars.map((b) => b.uses))
  return (
    <Glass tier="mid" hover className="flex h-[260px] flex-col overflow-hidden">
      <PanelHead
        title="Skill coverage"
        right={
          <span className="num text-[13px]" style={DISPLAY}>
            {active}/{total} active
          </span>
        }
      />
      <div className="flex-1 space-y-[6px] overflow-y-auto px-5 pb-4">
        {bars.slice(0, 8).map((b) => (
          <div key={b.name} className="relative h-5 overflow-hidden rounded-full bg-white/[0.06]">
            <div
              className="h-full rounded-full"
              style={{
                width: `${Math.max(14, (b.uses / max) * 100)}%`,
                background: `color-mix(in srgb, ${b.color} 55%, transparent)`,
              }}
            />
            <span className="absolute inset-0 flex items-center justify-between px-2 text-[11px]" style={MONO}>
              <span className="truncate">{b.name}</span>
              <span className="num" style={{ color: "var(--ink-2)" }}>
                {b.uses}
              </span>
            </span>
          </div>
        ))}
      </div>
    </Glass>
  )
}

// ---------------------------------------------------------------- brain tile
export function BrainTile({
  brain,
  edges,
}: {
  brain: { cards: number; notes: number; fresh: number; edges: number; hits: number; attempts: number }
  edges: [number, number][]
}) {
  const [hover, setHover] = useState(false)
  return (
    <a
      href="#/graph"
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      className="block"
    >
      <Glass tier="mid" hover className="flex h-[128px] items-center justify-between px-5">
        <div className="flex items-end gap-4">
          <span className="num text-[32px] leading-none" style={DISPLAY}>
            {brain.cards}
          </span>
          <div className="pb-1">
            <div className="num text-[13px]" style={{ color: "var(--ink-2)" }}>
              cards · {brain.notes} notes · {brain.edges} edges
            </div>
            <div className="mt-1 flex gap-1.5">
              <Chip color="var(--mint)">{brain.fresh} fresh 14d</Chip>
              <Chip color="var(--iris)">
                {brain.hits}/{brain.attempts} recall
              </Chip>
            </div>
          </div>
        </div>
        <div style={{ opacity: hover ? 1 : 0.75, transition: "opacity 200ms" }}>
          <Constellation nodes={16} edges={edges} />
        </div>
      </Glass>
    </a>
  )
}

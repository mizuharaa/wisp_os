/** Hand-rolled SVG marks. No chart library: every visualisation here is a
 * line, an area, a bar or a node set, and the spec bans the default chrome a
 * library would bring anyway. Shared rules: stroke 2, round caps, gradient
 * area 28% -> 0, one hairline baseline, no gridlines, no frames. */
import { useId } from "react"

const BASELINE = "rgba(255,255,255,0.08)"

function path(values: number[], w: number, h: number, pad = 1) {
  if (!values.length) return { line: "", area: "" }
  const max = Math.max(...values, 1)
  const step = values.length > 1 ? w / (values.length - 1) : w
  const pts = values.map((v, i) => [i * step, h - pad - (v / max) * (h - pad * 2)])
  const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("")
  return { line, area: `${line}L${w},${h}L0,${h}Z` }
}

export function Sparkline({
  values,
  color,
  width = 34,
  height = 18,
}: {
  values: number[]
  color: string
  width?: number
  height?: number
}) {
  const id = useId()
  const { line, area } = path(values, width, height)
  if (!line) return <svg width={width} height={height} aria-hidden />
  return (
    <svg width={width} height={height} className="w-chart overflow-visible" aria-hidden>
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.28" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${id})`} />
      <path
        d={line}
        fill="none"
        stroke={color}
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

/** Stacked area over a shared x axis. `series` is [{key, color, values}]. */
export function StackedArea({
  series,
  labels,
  height = 128,
}: {
  series: { key: string; color: string; values: number[] }[]
  labels: string[]
  height?: number
}) {
  const id = useId()
  const w = 1000
  const h = height
  const n = series[0]?.values.length ?? 0
  if (!n) return null
  const totals = new Array(n).fill(0)
  const bands = series.map((s) => {
    const top = s.values.map((v, i) => (totals[i] += v))
    return { ...s, top: [...top] }
  })
  const max = Math.max(...totals, 1)
  const step = n > 1 ? w / (n - 1) : w
  const y = (v: number) => h - (v / max) * (h - 4)

  return (
    <svg
      viewBox={`0 0 ${w} ${h + 16}`}
      preserveAspectRatio="none"
      className="w-chart h-full w-full"
      role="img"
      aria-label="usage burndown"
    >
      <defs>
        {bands.map((b, i) => (
          <linearGradient key={b.key} id={`${id}-${i}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={b.color} stopOpacity="0.28" />
            <stop offset="100%" stopColor={b.color} stopOpacity="0" />
          </linearGradient>
        ))}
      </defs>
      {bands
        .slice()
        .reverse()
        .map((b, ri) => {
          const i = bands.length - 1 - ri
          const line = b.top
            .map((v, k) => `${k ? "L" : "M"}${(k * step).toFixed(1)},${y(v).toFixed(1)}`)
            .join("")
          return (
            <g key={b.key}>
              <path d={`${line}L${w},${h}L0,${h}Z`} fill={`url(#${id}-${i})`} />
              <path
                d={line}
                fill="none"
                stroke={b.color}
                strokeWidth="2"
                strokeLinecap="round"
                vectorEffect="non-scaling-stroke"
              />
            </g>
          )
        })}
      <line x1="0" y1={h} x2={w} y2={h} stroke={BASELINE} strokeWidth="1" vectorEffect="non-scaling-stroke" />
      {labels.map((l, i) => (
        <text
          key={l + i}
          x={(i / Math.max(labels.length - 1, 1)) * w}
          y={h + 13}
          textAnchor={i === 0 ? "start" : i === labels.length - 1 ? "end" : "middle"}
          fill="rgba(244,242,255,0.40)"
          style={{ font: "10px var(--w-mono)" }}
        >
          {l}
        </text>
      ))}
    </svg>
  )
}

/** Thin progress arc for the connector card face. */
export function ProgressArc({
  pct,
  size = 54,
  color = "#fff",
}: {
  pct: number
  size?: number
  color?: string
}) {
  const r = size / 2 - 3
  const c = 2 * Math.PI * r
  return (
    <svg width={size} height={size} className="w-chart" aria-hidden>
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke="rgba(255,255,255,0.22)"
        strokeWidth="3"
      />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke={color}
        strokeWidth="3"
        strokeLinecap="round"
        strokeDasharray={`${(c * Math.min(pct, 100)) / 100} ${c}`}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
      />
    </svg>
  )
}

/** Static constellation laid out from real vault edges. */
export function Constellation({
  nodes,
  edges,
  width = 240,
  height = 84,
}: {
  nodes: number
  edges: [number, number][]
  width?: number
  height?: number
}) {
  const count = Math.min(nodes, 16)
  if (!count) return null
  // deterministic pseudo-random placement: same vault, same picture
  const pts = Array.from({ length: count }, (_, i) => {
    const a = (i * 2.399963) % (Math.PI * 2)
    const rad = 0.28 + ((i * 37) % 100) / 145
    return [
      width / 2 + Math.cos(a) * rad * (width / 2 - 8),
      height / 2 + Math.sin(a) * rad * (height / 2 - 8),
    ] as const
  })
  const drawn = edges
    .filter(([a, b]) => a < count && b < count && a !== b)
    .slice(0, 22)
  return (
    <svg width={width} height={height} className="w-chart" aria-hidden>
      {drawn.map(([a, b], i) => (
        <line
          key={i}
          x1={pts[a][0]}
          y1={pts[a][1]}
          x2={pts[b][0]}
          y2={pts[b][1]}
          stroke="var(--iris)"
          strokeOpacity="0.35"
          strokeWidth="1"
        />
      ))}
      {pts.map(([x, y], i) => (
        <circle key={i} cx={x} cy={y} r={i % 4 === 0 ? 3 : 2} fill="var(--iris)" fillOpacity={i % 4 === 0 ? 0.95 : 0.6} />
      ))}
    </svg>
  )
}

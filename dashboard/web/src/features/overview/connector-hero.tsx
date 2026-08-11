import { useState } from "react"
import { Chip, Glass, PanelHead } from "@/components/glass"
import { ProgressArc, Sparkline } from "@/components/viz"
import { countdown, type Connector } from "@/lib/overview"
import { useReducedMotion } from "@/lib/useReducedMotion"

/** The pattern breaker: the only solid, fully opaque, saturated surfaces on the
 * page. Laid out as a readable column -- the overlapping wallet stack hid the
 * numbers these cards exist to show. */
function CardFace({
  c,
  flipped,
  tilt,
}: {
  c: Connector
  flipped: boolean
  tilt: { rx: number; ry: number; mx: number; my: number } | null
}) {
  return (
    <div className="conn-inner" style={{ transform: flipped ? "rotateY(180deg)" : undefined }}>
      <div
        className="conn-side"
        style={{ background: `linear-gradient(135deg, ${c.face[0]}, ${c.face[1]})` }}
      >
        <div className="flex items-start justify-between">
          <div className="min-w-0 text-left">
            <div
              className="text-[15px] font-semibold"
              style={{ fontFamily: "var(--w-display)", letterSpacing: "-0.02em" }}
            >
              {c.name}
            </div>
            <div
              className="num mt-0.5 truncate text-[11px] opacity-80"
              style={{ fontFamily: "var(--w-mono)" }}
            >
              {c.handle}
            </div>
          </div>
          <Chip color="rgba(255,255,255,0.9)">{c.id}</Chip>
        </div>
        <div className="mt-auto flex items-end justify-between">
          <div className="text-left">
            <div className="num text-[34px] leading-none" style={{ fontFamily: "var(--w-display)" }}>
              {c.pct === null ? "--" : `${c.pct}%`}
            </div>
            <div className="num mt-1 text-[11px] opacity-80" style={{ fontFamily: "var(--w-mono)" }}>
              {c.resetAt ? `resets ${countdown(c.resetAt)}` : c.note}
            </div>
          </div>
          <ProgressArc pct={c.pct ?? 0} />
        </div>
      </div>
      <div
        className="conn-side conn-back"
        style={{ background: `linear-gradient(135deg, ${c.face[1]}, ${c.face[0]})` }}
      >
        <div className="num text-left text-[11px] opacity-80" style={{ fontFamily: "var(--w-mono)" }}>
          12h usage
        </div>
        {c.series && c.series.length > 1 ? (
          <Sparkline values={c.series} color="rgba(255,255,255,0.95)" width={240} height={30} />
        ) : (
          <div className="num py-2 text-[11px] opacity-70" style={{ fontFamily: "var(--w-mono)" }}>
            recording — not enough samples yet
          </div>
        )}
        <dl
          className="num mt-auto grid grid-cols-3 gap-2 text-left text-[11px]"
          style={{ fontFamily: "var(--w-mono)" }}
        >
          <div>
            <dt className="opacity-70">accounts</dt>
            <dd>{c.sessions ?? "--"}</dd>
          </div>
          <div>
            <dt className="opacity-70">last sync</dt>
            <dd>{c.lastSync}</dd>
          </div>
          <div>
            <dt className="opacity-70">window</dt>
            <dd>{c.resetAt ? countdown(c.resetAt) : "--"}</dd>
          </div>
        </dl>
      </div>
      {tilt && (
        <div
          className="conn-sheen"
          style={{
            background: `radial-gradient(180px circle at ${tilt.mx}% ${tilt.my}%, rgba(255,255,255,0.18), transparent 60%)`,
          }}
        />
      )}
    </div>
  )
}

export function ConnectorHero({ connectors }: { connectors: Connector[] }) {
  const [hover, setHover] = useState<string | null>(null)
  const [flipped, setFlipped] = useState<Record<string, boolean>>({})
  const [tilt, setTilt] = useState<{ rx: number; ry: number; mx: number; my: number } | null>(null)
  const reduced = useReducedMotion()

  return (
    <Glass tier="mid" className="flex h-[340px] flex-col overflow-hidden">
      <PanelHead
        title="Connectors"
        right={<Chip color="var(--ink-3)">click for history</Chip>}
      />
      <div
        className="flex flex-1 flex-col gap-3 overflow-y-auto px-5 pb-5"
        style={{ perspective: 1200 }}
        onMouseLeave={() => {
          setHover(null)
          setTilt(null)
        }}
      >
        {connectors.map((c) => {
          const t = hover === c.id ? tilt : null
          return (
            <button
              key={c.id}
              type="button"
              className="conn-card press"
              style={{
                transform: t && !reduced ? `rotateX(${t.rx}deg) rotateY(${t.ry}deg)` : undefined,
              }}
              onMouseEnter={() => setHover(c.id)}
              onMouseMove={(e) => {
                if (reduced) return
                const r = e.currentTarget.getBoundingClientRect()
                const px = (e.clientX - r.left) / r.width
                const py = (e.clientY - r.top) / r.height
                setTilt({ rx: (0.5 - py) * 12, ry: (px - 0.5) * 12, mx: px * 100, my: py * 100 })
              }}
              onClick={() => setFlipped((f) => ({ ...f, [c.id]: !f[c.id] }))}
            >
              <CardFace c={c} flipped={!reduced && !!flipped[c.id]} tilt={t} />
            </button>
          )
        })}
      </div>
    </Glass>
  )
}

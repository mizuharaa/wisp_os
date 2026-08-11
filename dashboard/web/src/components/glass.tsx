import { cn } from "@/lib/utils"

type Tier = "strong" | "mid" | "light"

/** The only place glass CSS is declared (see globals.css's .glass-*).
 * `root` opts a light-tier surface into its own backdrop-filter -- use it only
 * when the surface is NOT nested inside another blurring panel. */
export function Glass({
  tier = "mid",
  root = false,
  hover = false,
  className,
  ...props
}: React.ComponentProps<"div"> & { tier?: Tier; root?: boolean; hover?: boolean }) {
  return (
    <div
      className={cn(
        "glass",
        `glass-${tier}`,
        root && "glass-root",
        hover && "glass-hover",
        className,
      )}
      {...props}
    />
  )
}

/** 22px pill, 11px mono, background at 14% of its own colour. */
export function Chip({
  color = "var(--ink-2)",
  className,
  children,
  ...props
}: React.ComponentProps<"span"> & { color?: string }) {
  return (
    <span
      className={cn(
        "num inline-flex h-[22px] shrink-0 items-center rounded-full px-2 text-[11px]",
        className,
      )}
      style={{
        color,
        background: `color-mix(in srgb, ${color} 14%, transparent)`,
        fontFamily: "var(--w-mono)",
      }}
      {...props}
    >
      {children}
    </span>
  )
}

/** Placeholder data must never be silently faked. */
export function MockChip({ color = "var(--slate)" }: { color?: string }) {
  return (
    <Chip color={color} title="placeholder data — no source wired yet">
      mock
    </Chip>
  )
}

export function PanelHead({
  title,
  mock = false,
  right,
}: {
  title: string
  mock?: boolean
  right?: React.ReactNode
}) {
  return (
    <div className="flex items-center justify-between gap-3 px-5 pt-4 pb-3">
      <div className="flex items-center gap-2">
        <h2
          className="text-[13px] font-semibold"
          style={{ fontFamily: "var(--w-display)", letterSpacing: "-0.02em" }}
        >
          {title}
        </h2>
        {mock && <MockChip />}
      </div>
      {right}
    </div>
  )
}

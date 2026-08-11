import { useEffect, useState } from "react"
import {
  LayoutDashboard,
  Calendar,
  Terminal,
  GitBranch,
  BrainCircuit,
  Share2,
  Plug,
  ScrollText,
  ShieldCheck,
} from "lucide-react"
import { ROUTE_LABELS, ROUTE_ORDER, type RouteId } from "@/routes"

/** Lifted from the portfolio site's Navbar (PortfolioWebsite/src/app/components
 * /Navbar.tsx): a top bar that contracts into a centred pill once the page is
 * scrolled past 60px, dropping its labels down to icons. Same threshold, same
 * transition curve and durations; only the palette is Wisp's. */
const ICONS: Record<RouteId, React.ComponentType<{ className?: string; strokeWidth?: number }>> = {
  overview: LayoutDashboard,
  calendar: Calendar,
  instances: Terminal,
  skills: GitBranch,
  brain: BrainCircuit,
  graph: Share2,
  integrations: Plug,
  audit: ScrollText,
  guard: ShieldCheck,
}

export function TopNav({ route }: { route: RouteId }) {
  const [scrolled, setScrolled] = useState(false)

  useEffect(() => {
    const handleScroll = () => setScrolled(window.scrollY > 60)
    window.addEventListener("scroll", handleScroll, { passive: true })
    return () => window.removeEventListener("scroll", handleScroll)
  }, [])

  return (
    <div className="fixed top-4 right-0 left-0 z-50 px-4 md:px-8">
      <nav
        className={`absolute flex items-center overflow-hidden rounded-2xl border shadow-lg backdrop-blur-md ${
          scrolled
            ? "left-[calc(50%-210px)] right-[calc(50%-210px)] gap-0.5 border-white/10 bg-[#14102a]/90 px-3 py-2"
            : "left-4 right-4 gap-6 border-white/20 bg-white/10 px-6 py-4 md:left-8 md:right-8 md:px-10"
        }`}
        style={{
          transition:
            "left 0.55s cubic-bezier(0.34, 1.2, 0.64, 1), right 0.55s cubic-bezier(0.34, 1.2, 0.64, 1), padding 0.55s ease-out, gap 0.55s ease-out, background-color 0.45s ease-out, border-color 0.45s ease-out",
        }}
      >
        {/* Brand area */}
        <div className="flex flex-shrink-0 items-center gap-3">
          {scrolled ? (
            <div className="flex size-8 flex-shrink-0 items-center justify-center rounded-full bg-[var(--iris)]">
              <span
                className="text-sm leading-none font-bold text-[#16112c]"
                style={{ fontFamily: "var(--w-display)" }}
              >
                W
              </span>
            </div>
          ) : (
            <>
              <div className="flex size-9 flex-shrink-0 items-center justify-center rounded-full border-2 border-white/20 bg-[var(--iris)]">
                <span
                  className="text-sm leading-none font-bold text-[#16112c]"
                  style={{ fontFamily: "var(--w-display)" }}
                >
                  W
                </span>
              </div>
              <span
                className="text-sm font-semibold whitespace-nowrap"
                style={{ fontFamily: "var(--w-display)", letterSpacing: "-0.02em" }}
              >
                Wisp
              </span>
            </>
          )}
        </div>

        {/* Nav items */}
        <div
          className={`flex min-w-0 flex-1 items-center justify-center ${
            scrolled ? "gap-0.5" : "gap-1"
          }`}
          style={{ transition: "gap 0.5s ease-out" }}
        >
          {ROUTE_ORDER.map((id) => {
            const Icon = ICONS[id]
            const active = route === id
            return (
              <a
                key={id}
                href={`#/${id}`}
                aria-current={active ? "page" : undefined}
                title={scrolled ? ROUTE_LABELS[id] : undefined}
                className={`group relative flex flex-shrink-0 items-center rounded-lg transition-all duration-300 ${
                  scrolled
                    ? `p-2 ${active ? "bg-[var(--iris)] text-[#16112c]" : "text-white/60 hover:bg-[var(--iris)] hover:text-[#16112c]"}`
                    : `px-3 py-2 ${active ? "bg-[var(--iris)] text-[#16112c]" : "text-white/70 hover:bg-white/10 hover:text-white"}`
                }`}
              >
                <Icon className="size-4 flex-shrink-0" strokeWidth={2} />
                {!scrolled && (
                  <span className="ml-2 text-[13px] font-medium whitespace-nowrap">
                    {ROUTE_LABELS[id]}
                  </span>
                )}
              </a>
            )
          })}
        </div>

        {/* Action */}
        <div className="flex flex-shrink-0 items-center gap-2">
          <a
            href="#/instances"
            title="Agent console"
            className={`flex size-9 items-center justify-center rounded-full transition-colors duration-200 ${
              scrolled
                ? "bg-[var(--iris)] text-[#16112c] hover:bg-[#a294ff]"
                : "bg-white/90 text-[#16112c] hover:bg-[var(--iris)]"
            }`}
          >
            <Terminal className="size-4" strokeWidth={2} />
          </a>
        </div>
      </nav>
    </div>
  )
}

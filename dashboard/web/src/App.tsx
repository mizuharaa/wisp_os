import { TooltipProvider } from "@/components/ui/tooltip"
import { TopNav } from "@/components/top-nav"
import { DashboardDataProvider } from "@/lib/useDashboardData"
import { useHashRoute } from "@/lib/useHashRoute"
import { InstancesPage } from "@/pages/instances-page"
import { OverviewPage } from "@/pages/overview-page"
import { PlaceholderPage } from "@/pages/placeholder-page"
import { SkillsPage } from "@/pages/skills-page"
import { PORTED_ROUTES, ROUTE_LABELS, ROUTE_ORDER, type RouteId } from "@/routes"

function RouteOutlet({ route }: { route: RouteId }) {
  if (route === "overview") return <OverviewPage />
  if (route === "instances") return <InstancesPage />
  if (route === "skills") return <SkillsPage />
  return <PlaceholderPage title={ROUTE_LABELS[route]} />
}

function Shell() {
  const route = useHashRoute(ROUTE_ORDER, "overview") as RouteId
  return (
    <div className="wisp-shell relative min-h-dvh">
      {/* the backdrop is the light source: fixed, directional, never animated */}
      <div className="wisp-backdrop" />
      <div className="wisp-grain" />
      <TopNav route={route} />
      {/* the bar floats above this plane; the offset clears it at rest */}
      <main className="relative z-10 pt-[92px]">
        <RouteOutlet route={route} />
        {!PORTED_ROUTES.has(route) && (
          <p className="num px-7 pb-7 text-[11px]" style={{ fontFamily: "var(--w-mono)", color: "var(--ink-3)" }}>
            {ROUTE_LABELS[route]} still lives in the classic dashboard
          </p>
        )}
      </main>
    </div>
  )
}

function App() {
  return (
    <TooltipProvider>
      <DashboardDataProvider>
        <Shell />
      </DashboardDataProvider>
    </TooltipProvider>
  )
}

export default App

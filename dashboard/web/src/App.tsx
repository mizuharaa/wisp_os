import { SidebarInset, SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar"
import { TooltipProvider } from "@/components/ui/tooltip"
import { Separator } from "@/components/ui/separator"
import { NavRail } from "@/components/nav-rail"
import { DashboardDataProvider } from "@/lib/useDashboardData"
import { useHashRoute } from "@/lib/useHashRoute"
import { InstancesPage } from "@/pages/instances-page"
import { PlaceholderPage } from "@/pages/placeholder-page"
import { SkillsPage } from "@/pages/skills-page"
import { PORTED_ROUTES, ROUTE_LABELS, ROUTE_ORDER, type RouteId } from "@/routes"

function RouteOutlet({ route }: { route: RouteId }) {
  if (route === "instances") return <InstancesPage />
  if (route === "skills") return <SkillsPage />
  return <PlaceholderPage title={ROUTE_LABELS[route]} />
}

function Shell() {
  const route = useHashRoute(ROUTE_ORDER, "overview") as RouteId
  return (
    <SidebarProvider>
      <NavRail route={route} />
      <SidebarInset>
        <header className="flex h-12 items-center gap-2 border-b border-border px-3">
          <SidebarTrigger />
          <Separator orientation="vertical" className="h-5" />
          <span className="font-heading text-sm font-medium">{ROUTE_LABELS[route]}</span>
          {!PORTED_ROUTES.has(route) && (
            <span className="text-xs text-muted-foreground">(classic dashboard)</span>
          )}
        </header>
        <RouteOutlet route={route} />
      </SidebarInset>
    </SidebarProvider>
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

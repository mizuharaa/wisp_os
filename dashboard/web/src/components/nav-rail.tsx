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
import {
  Sidebar,
  SidebarContent,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar"
import { ThemeToggle } from "@/components/theme-toggle"
import { ROUTE_LABELS, ROUTE_ORDER, type RouteId } from "@/routes"

const ICONS: Record<RouteId, React.ComponentType<{ className?: string }>> = {
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

export function NavRail({
  route,
  counts = {},
}: {
  route: RouteId
  counts?: Partial<Record<RouteId, number>>
}) {
  return (
    <Sidebar collapsible="icon">
      <SidebarHeader className="flex flex-row items-center justify-between px-2 py-1.5">
        <span className="px-1 font-heading text-sm font-semibold group-data-[collapsible=icon]:hidden">
          Rune
        </span>
        <ThemeToggle />
      </SidebarHeader>
      <SidebarContent>
        <SidebarMenu>
          {ROUTE_ORDER.map((id) => {
            const Icon = ICONS[id]
            const count = counts[id]
            return (
              <SidebarMenuItem key={id}>
                <SidebarMenuButton asChild isActive={route === id} tooltip={ROUTE_LABELS[id]}>
                  <a href={`#/${id}`}>
                    <Icon />
                    <span>{ROUTE_LABELS[id]}</span>
                  </a>
                </SidebarMenuButton>
                {!!count && <SidebarMenuBadge>{count}</SidebarMenuBadge>}
              </SidebarMenuItem>
            )
          })}
        </SidebarMenu>
      </SidebarContent>
    </Sidebar>
  )
}

import { MissionChat } from "@/features/instances/chat/mission-chat"
import { DirectiveInbox } from "@/features/instances/directive-inbox"
import { OrchestratorLoops } from "@/features/instances/orchestrator-loops"
import { WireSessions } from "@/features/instances/wire-sessions"
import { useDashboardData } from "@/lib/useDashboardData"

// Terminals land here in a later pass -- not part of the reported bugs.
export function InstancesPage() {
  const { wire } = useDashboardData()
  return (
    <div className="flex flex-col gap-6 p-6">
      <div>
        <h1 className="font-heading text-xl font-semibold">Agent console</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {wire ? "Connected" : "Connection lost — retrying…"}
        </p>
      </div>
      <MissionChat />
      <details className="group">
        <summary className="cursor-pointer font-heading text-sm font-semibold text-muted-foreground">
          Conductor loop, sessions &amp; inbox
        </summary>
        <div className="mt-4 flex flex-col gap-6">
          <OrchestratorLoops />
          <WireSessions />
          <DirectiveInbox />
        </div>
      </details>
    </div>
  )
}

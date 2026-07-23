import { useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { postOrchAction, type OrchLoop } from "@/lib/api"
import { useDashboardData } from "@/lib/useDashboardData"

const STATUS_TONE: Record<string, string> = {
  done: "bg-success text-white",
  waiting: "bg-warning text-white",
  running: "bg-info text-white",
  rejected: "bg-danger text-white",
  error: "bg-danger text-white",
  stalled: "bg-warning text-white",
  exhausted: "bg-warning text-white",
  stopped: "bg-secondary text-secondary-foreground",
}

function LoopCard({ loop, onChanged }: { loop: OrchLoop; onChanged: () => void }) {
  const [busy, setBusy] = useState(false)
  const waiting = loop.status === "waiting" && loop.live
  const run = async (action: string, feedback = "") => {
    setBusy(true)
    try {
      await postOrchAction(loop.oid, action, feedback)
      await onChanged()
    } finally {
      setBusy(false)
    }
  }
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-2 space-y-0">
        <CardTitle className="font-heading text-sm font-medium">{loop.name}</CardTitle>
        <Badge className={STATUS_TONE[loop.status] ?? "bg-secondary text-secondary-foreground"}>
          {loop.status}
        </Badge>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <p className="text-xs text-muted-foreground">
          {loop.oid} · round {String(loop.round ?? "?")}/{String(loop.rounds ?? "?")} · worker{" "}
          {String(loop.model)} · critic {String(loop.critic)} · $
          {(Number(loop.cost) || 0).toFixed(3)}
        </p>
        <div className="flex flex-wrap gap-2">
          {waiting && (
            <>
              <Button size="sm" disabled={busy} onClick={() => run("accept")}>
                Accept
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() => {
                  const feedback = window.prompt("Next instruction for the worker:") || ""
                  if (feedback) run("revise", feedback)
                }}
              >
                Revise
              </Button>
              <Button
                size="sm"
                variant="destructive"
                disabled={busy}
                onClick={() => run("reject")}
              >
                Reject
              </Button>
            </>
          )}
          {loop.live && (
            <Button size="sm" variant="destructive" disabled={busy} onClick={() => run("stop")}>
              Stop
            </Button>
          )}
          {!loop.live && (
            <Button size="sm" variant="outline" disabled={busy} onClick={() => run("dismiss")}>
              Dismiss
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

export function OrchestratorLoops() {
  const { orchestrations, refetch } = useDashboardData()
  return (
    <section className="flex flex-col gap-3">
      <h2 className="font-heading text-sm font-semibold text-muted-foreground">
        Conductor loop
      </h2>
      {orchestrations.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No loops yet — pick "Orchestrated" in the launcher and the core runs the mission
          hands-free.
        </p>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {orchestrations.map((loop) => (
            <LoopCard key={loop.oid} loop={loop} onChanged={refetch} />
          ))}
        </div>
      )}
    </section>
  )
}

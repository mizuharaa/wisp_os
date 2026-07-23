import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { deriveSessions } from "@/lib/derive"
import { useDashboardData } from "@/lib/useDashboardData"
import { useDismissedIds } from "@/lib/useDismissedIds"

export function WireSessions() {
  const { wireEvents, instances } = useDashboardData()
  const { isDismissed, dismiss } = useDismissedIds("wire-sessions")

  const named = new Set(instances.map((w) => w.sid))
  // Only sessions Rune didn't launch -- launched ones already show on their
  // own terminal cards.
  const outside = deriveSessions(wireEvents)
    .filter((s) => !named.has(s.id))
    .filter((s) => !isDismissed(s.id))

  return (
    <section className="flex flex-col gap-3">
      <h2 className="font-heading text-sm font-semibold text-muted-foreground">
        Outside sessions
      </h2>
      {outside.length === 0 ? (
        <p className="text-sm text-muted-foreground">nothing reporting yet</p>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {outside.map((s) => (
            <Card key={s.id} className="min-w-0">
              <CardContent className="flex min-w-0 flex-col gap-2">
                <div className="flex min-w-0 items-center justify-between gap-2">
                  <span className="min-w-0 truncate text-sm font-medium" title={s.job}>
                    {s.job || "idle conductor"}
                  </span>
                  <Badge className={s.live ? "bg-success text-white" : "bg-secondary text-secondary-foreground"}>
                    {s.live ? "Live" : "Idle"}
                  </Badge>
                </div>
                <p className="min-w-0 truncate text-xs text-muted-foreground">
                  {s.id} · last seen {(s.last || "").slice(11, 19) || "?"}
                </p>
                {s.lastline && (
                  <p className="min-w-0 truncate text-xs text-muted-foreground">▸ {s.lastline}</p>
                )}
                <Button
                  size="sm"
                  variant="outline"
                  className="self-start"
                  onClick={() => dismiss(s.id)}
                >
                  Dismiss
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </section>
  )
}

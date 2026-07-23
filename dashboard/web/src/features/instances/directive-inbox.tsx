import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { deriveDoneIds } from "@/lib/derive"
import { useDashboardData } from "@/lib/useDashboardData"
import { useDismissedIds } from "@/lib/useDismissedIds"

export function DirectiveInbox() {
  const { directives, wireEvents } = useDashboardData()
  const { isDismissed, dismiss } = useDismissedIds("directive-inbox")
  const done = deriveDoneIds(wireEvents)

  const entries = directives
    .slice(-10)
    .reverse()
    .filter((d) => !isDismissed(String(d.id)))

  return (
    <section className="flex flex-col gap-3">
      <h2 className="font-heading text-sm font-semibold text-muted-foreground">
        Directive inbox
      </h2>
      {entries.length === 0 ? (
        <p className="text-sm text-muted-foreground">no directives queued</p>
      ) : (
        <div className="flex flex-col gap-2">
          {entries.map((d) => {
            const id = String(d.id)
            const isDone = done.has(id)
            return (
              <div
                key={id}
                className="flex items-center gap-3 rounded-lg border border-border bg-card p-3"
              >
                <span className="font-mono text-[11px] text-muted-foreground">{id}</span>
                <span className="min-w-0 flex-1 truncate text-sm">{String(d.text ?? "")}</span>
                <Badge className={isDone ? "bg-success text-white" : "bg-secondary text-secondary-foreground"}>
                  {isDone ? "Done" : "Queued"}
                </Badge>
                <Button size="sm" variant="outline" onClick={() => dismiss(id)}>
                  Dismiss
                </Button>
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

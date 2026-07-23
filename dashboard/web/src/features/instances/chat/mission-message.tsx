import { Badge } from "@/components/ui/badge"
import type { CeoRole } from "@/lib/api"
import type { Exchange } from "@/lib/deriveExchanges"
import { renderMarkdown } from "@/lib/markdown"

const STATUS_TONE: Record<string, string> = {
  done: "bg-success text-white",
  working: "bg-info text-white",
  running: "bg-info text-white",
  retrying: "bg-info text-white",
  repairing: "bg-info text-white",
  review: "bg-warning text-white",
  waiting_permission: "bg-warning text-white",
  planning: "bg-info text-white",
  failed: "bg-danger text-white",
  blocked: "bg-danger text-white",
  exhausted: "bg-warning text-white",
  stopped: "bg-secondary text-secondary-foreground",
  pending: "bg-secondary text-secondary-foreground",
  skipped: "bg-secondary text-secondary-foreground",
}

function tone(status: string): string {
  return STATUS_TONE[status] ?? "bg-secondary text-secondary-foreground"
}

function RoleRow({ role }: { role: CeoRole }) {
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-lg border border-border bg-card p-2">
      <div className="flex min-w-0 items-center justify-between gap-2">
        <span className="min-w-0 truncate text-xs font-medium">{role.title || role.id}</span>
        <Badge className={tone(role.status)}>{role.status}</Badge>
      </div>
      <p className="text-[11px] text-muted-foreground">
        {role.model || "?"} · {role.turns ?? "?"} turns
        {typeof role.cost === "number" ? ` · $${role.cost.toFixed(3)}` : ""}
      </p>
      {role.status === "done" && role.result && (
        <div
          className="md text-xs [&_h4]:mt-1 [&_h4]:font-semibold [&_p]:mt-1 [&_pre]:mt-1 [&_pre]:overflow-x-auto [&_pre]:rounded [&_pre]:bg-muted [&_pre]:p-2"
          dangerouslySetInnerHTML={{ __html: renderMarkdown(role.result) }}
        />
      )}
    </div>
  )
}

export function MissionMessage({ exchange }: { exchange: Exchange }) {
  const expandable = exchange.route === "delegate" && exchange.roles.length > 1

  return (
    <article className="flex flex-col gap-2">
      <div className="flex min-w-0 items-start justify-between gap-2">
        <p className="min-w-0 flex-1 whitespace-pre-wrap text-sm font-medium">{exchange.prompt}</p>
        <Badge className={tone(exchange.status)}>{exchange.live ? "live" : exchange.status}</Badge>
      </div>
      {expandable ? (
        <details className="rounded-lg border border-border bg-card p-3">
          <summary className="cursor-pointer text-sm text-muted-foreground">
            {exchange.reply}
          </summary>
          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            {exchange.roles.map((role) => (
              <RoleRow key={role.id} role={role} />
            ))}
          </div>
        </details>
      ) : (
        <div
          className="md rounded-lg border border-border bg-card p-3 text-sm [&_h4]:mt-2 [&_h4]:font-semibold [&_p]:mt-1 [&_pre]:mt-2 [&_pre]:overflow-x-auto [&_pre]:rounded [&_pre]:bg-muted [&_pre]:p-2 [&_table]:mt-2"
          dangerouslySetInnerHTML={{ __html: renderMarkdown(exchange.reply) }}
        />
      )}
      {exchange.cost > 0 && (
        <p className="text-[11px] text-muted-foreground">${exchange.cost.toFixed(3)}</p>
      )}
    </article>
  )
}

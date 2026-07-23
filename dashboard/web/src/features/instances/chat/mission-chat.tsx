import { useEffect, useRef, useState } from "react"
import { Card } from "@/components/ui/card"
import { postCeoMessage, type CeoOpts } from "@/lib/api"
import { deriveExchanges } from "@/lib/deriveExchanges"
import { useDashboardData } from "@/lib/useDashboardData"
import { Composer } from "@/features/instances/chat/composer"
import { MissionMessage } from "@/features/instances/chat/mission-message"

interface PendingMessage {
  localId: string
  prompt: string
  status: "sending" | "sent" | "error"
  cid?: string
  error?: string
}

let pendingSeq = 0

export function MissionChat() {
  const { ceoRuns, ceoHistory, refetch } = useDashboardData()
  const [pending, setPending] = useState<PendingMessage[]>([])
  const listRef = useRef<HTMLDivElement>(null)

  const exchanges = deriveExchanges(ceoRuns, ceoHistory)
  const knownCids = new Set(exchanges.map((e) => e.cid))

  // Drop pending entries once the real record has arrived via polling.
  useEffect(() => {
    setPending((prev) => prev.filter((p) => !(p.cid && knownCids.has(p.cid))))
    // knownCids is a pure derivation of ceoRuns/ceoHistory recomputed fresh
    // every render (not a stale ref) -- depending on those two, not the Set
    // object itself (a new reference every render), is what avoids re-running
    // this effect, and re-filtering an already-stable array, on every render.
  }, [ceoRuns, ceoHistory])

  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [exchanges.length, pending.length])

  const send = async (text: string, opts: CeoOpts) => {
    const localId = `pending-${++pendingSeq}`
    setPending((prev) => [...prev, { localId, prompt: text, status: "sending" }])
    try {
      const result = await postCeoMessage(text, opts)
      setPending((prev) =>
        prev.map((p) => (p.localId === localId ? { ...p, status: "sent", cid: result.cid } : p)),
      )
      refetch()
    } catch (error) {
      setPending((prev) =>
        prev.map((p) =>
          p.localId === localId
            ? { ...p, status: "error", error: error instanceof Error ? error.message : "Send failed" }
            : p,
        ),
      )
    }
  }

  return (
    <Card className="flex min-h-[560px] flex-col overflow-hidden p-0">
      <div ref={listRef} className="flex flex-1 min-h-0 flex-col gap-4 overflow-y-auto p-4">
        {exchanges.length === 0 && pending.length === 0 ? (
          <p className="m-auto text-sm text-muted-foreground">
            No missions yet — tell the CEO what to do.
          </p>
        ) : (
          <>
            {exchanges.map((exchange) => (
              <MissionMessage key={exchange.cid} exchange={exchange} />
            ))}
            {pending.map((p) => (
              <article key={p.localId} className="flex flex-col gap-2">
                <p className="whitespace-pre-wrap text-sm font-medium">{p.prompt}</p>
                <p className="text-sm text-muted-foreground">
                  {p.status === "sending" && "Sending…"}
                  {p.status === "sent" && "Working…"}
                  {p.status === "error" && (
                    <span className="text-danger">{p.error}</span>
                  )}
                </p>
              </article>
            ))}
          </>
        )}
      </div>
      <Composer busy={pending.some((p) => p.status === "sending")} onSend={send} />
    </Card>
  )
}

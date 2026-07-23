import { useCallback, useState } from "react"

const MAX_IDS = 200

function load(key: string): Set<string> {
  try {
    const raw = localStorage.getItem(key)
    return new Set(raw ? (JSON.parse(raw) as string[]) : [])
  } catch {
    return new Set()
  }
}

function persist(key: string, ids: Set<string>) {
  try {
    // Bounded so a long-lived browser tab can't grow this without limit.
    localStorage.setItem(key, JSON.stringify([...ids].slice(-MAX_IDS)))
  } catch {
    // storage unavailable (private mode, quota) -- dismiss still works this session
  }
}

/** Client-only dismiss, same mechanism dashboard/index.html's CMD.items
 * filtering already uses for the command tray -- persisted to localStorage
 * so it survives the next poll instead of living in an in-memory array.
 * Backs wire-sessions and the directive inbox, neither of which is an
 * individually-addressable, mutable server record (sessions are derived
 * from the event log every poll; directives are lines in an append-only
 * jsonl file), so there is no backend endpoint to call here. */
export function useDismissedIds(storageKey: string) {
  const key = `rune-dismissed:${storageKey}`
  const [dismissed, setDismissed] = useState<Set<string>>(() => load(key))

  const dismiss = useCallback(
    (id: string) => {
      setDismissed((prev) => {
        const next = new Set(prev)
        next.add(id)
        persist(key, next)
        return next
      })
    },
    [key],
  )

  const isDismissed = useCallback((id: string) => dismissed.has(id), [dismissed])

  return { isDismissed, dismiss }
}

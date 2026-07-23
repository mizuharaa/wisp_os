import { useEffect, useState } from "react"

/** Minimal hash router: 9 flat routes, no params, no nesting. */
export function useHashRoute(validIds: readonly string[], fallback: string) {
  const resolve = () => {
    const id = location.hash.slice(2) || fallback
    return validIds.includes(id) ? id : fallback
  }
  const [route, setRoute] = useState(resolve)
  useEffect(() => {
    const onHashChange = () => setRoute(resolve())
    window.addEventListener("hashchange", onHashChange)
    return () => window.removeEventListener("hashchange", onHashChange)
  }, [])
  return route
}

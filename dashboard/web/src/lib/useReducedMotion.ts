import { useEffect, useState } from "react"

/** Live-updating version of dashboard/index.html's one-time `reduced`
 * matchMedia snapshot (index.html:1738) -- a small correctness improvement
 * (reacts if the OS setting changes mid-session) over the legacy snapshot,
 * cheap to add since React re-renders are already idiomatic here. */
export function useReducedMotion(): boolean {
  const query = "(prefers-reduced-motion: reduce)"
  const [reduced, setReduced] = useState(() => matchMedia(query).matches)
  useEffect(() => {
    const mql = matchMedia(query)
    const onChange = () => setReduced(mql.matches)
    mql.addEventListener("change", onChange)
    return () => mql.removeEventListener("change", onChange)
  }, [])
  return reduced
}

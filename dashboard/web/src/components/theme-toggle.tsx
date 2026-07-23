import { useEffect, useState } from "react"
import { Moon, Sun } from "lucide-react"
import { Button } from "@/components/ui/button"

type Theme = "light" | "dark"

/** Reuses dashboard/index.html's exact contract: localStorage["rune-theme"]
 * + document.documentElement.dataset.theme -- so tokens.css's
 * [data-theme="dark"] selector and the legacy app's own toggle keep working
 * unmodified while both frontends coexist during the migration. */
function currentTheme(): Theme {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light"
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(currentTheme)

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try {
      localStorage.setItem("rune-theme", theme)
    } catch {
      // storage unavailable (private mode etc.) -- theme still applies this session
    }
  }, [theme])

  return (
    <Button
      variant="ghost"
      size="icon"
      aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
      onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
    >
      {theme === "dark" ? <Sun /> : <Moon />}
    </Button>
  )
}

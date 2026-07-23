import { useState } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { postAddSkill } from "@/lib/api"
import { useDashboardData } from "@/lib/useDashboardData"

const BRANCHES = ["engineering", "design", "ops", "meta", "misc"]

export function AddSkillForm() {
  const { refetch } = useDashboardData()
  const [name, setName] = useState("")
  const [branch, setBranch] = useState("misc")
  const [trigger, setTrigger] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  const submit = async () => {
    if (!name.trim() || busy) return
    setBusy(true)
    setError("")
    try {
      await postAddSkill(name.trim(), branch, trigger.trim() || name.trim())
      setName("")
      setTrigger("")
      await refetch()
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to add skill")
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <h3 className="font-heading text-sm font-semibold text-muted-foreground">
        Add a skill <span className="font-normal">· admin · seeds a learning node (earns after 3 uses)</span>
      </h3>
      <div className="flex flex-wrap items-center gap-2">
        <Input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="skill name — e.g. 'data-pipeline'"
          maxLength={40}
          className="w-56"
        />
        <Select value={branch} onValueChange={setBranch}>
          <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
          <SelectContent>
            {BRANCHES.map((b) => (
              <SelectItem key={b} value={b}>{b}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Input
          value={trigger}
          onChange={(e) => setTrigger(e.target.value)}
          placeholder="/trigger (optional)"
          className="w-40"
        />
        <Button onClick={submit} disabled={busy || !name.trim()}>
          Add skill
        </Button>
      </div>
      {error && <p className="text-xs text-danger">{error}</p>}
    </div>
  )
}

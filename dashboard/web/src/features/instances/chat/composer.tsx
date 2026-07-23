import { useState } from "react"
import { SlidersHorizontal, Send } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import type { CeoOpts } from "@/lib/api"

const DEFAULT_OPTS: CeoOpts = {
  mode: "auto",
  refine: "auto",
  model: "auto",
  effort: "auto",
  account: "auto",
  gate: false,
}

export function Composer({
  busy,
  onSend,
}: {
  busy: boolean
  onSend: (text: string, opts: CeoOpts) => void
}) {
  const [text, setText] = useState("")
  const [opts, setOpts] = useState<CeoOpts>(DEFAULT_OPTS)

  const send = () => {
    const value = text.trim()
    if (!value || busy) return
    onSend(value, opts)
    setText("")
  }

  return (
    <div className="flex flex-col gap-2 border-t border-border p-3">
      <div className="flex items-end gap-2">
        <Textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault()
              send()
            }
          }}
          placeholder="Tell the CEO what to do… (Shift+Enter for a new line)"
          className="min-h-[44px] flex-1 resize-none"
          disabled={busy}
        />
        <Popover>
          <PopoverTrigger asChild>
            <Button variant="outline" size="icon" title="Advanced: model, effort & account overrides">
              <SlidersHorizontal />
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" className="flex w-72 flex-col gap-3">
            <p className="text-xs font-medium text-muted-foreground">
              CEO overrides · auto = the CEO decides per prompt
            </p>
            <OptRow label="Mode">
              <Select value={opts.mode} onValueChange={(v) => setOpts((o) => ({ ...o, mode: v as CeoOpts["mode"] }))}>
                <SelectTrigger className="h-8"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="auto">Auto · route per prompt</SelectItem>
                  <SelectItem value="solo">Solo · run it, 1 agent, no subagents</SelectItem>
                  <SelectItem value="answer">Answer only · chat, no tools</SelectItem>
                  <SelectItem value="delegate">Delegate · CEO staffs a roster</SelectItem>
                </SelectContent>
              </Select>
            </OptRow>
            <OptRow label="Refine">
              <Select value={opts.refine} onValueChange={(v) => setOpts((o) => ({ ...o, refine: v as CeoOpts["refine"] }))}>
                <SelectTrigger className="h-8"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="auto">Auto · only short prompts</SelectItem>
                  <SelectItem value="off">Off · use my words as-is</SelectItem>
                </SelectContent>
              </Select>
            </OptRow>
            <OptRow label="Model">
              <Select value={opts.model} onValueChange={(v) => setOpts((o) => ({ ...o, model: v as CeoOpts["model"] }))}>
                <SelectTrigger className="h-8"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="auto">Auto · per role</SelectItem>
                  <SelectItem value="haiku">Haiku · all roles</SelectItem>
                  <SelectItem value="sonnet">Sonnet · all roles</SelectItem>
                  <SelectItem value="opus">Opus · all roles</SelectItem>
                  <SelectItem value="fable">Fable · all roles</SelectItem>
                </SelectContent>
              </Select>
            </OptRow>
            <OptRow label="Effort">
              <Select value={opts.effort} onValueChange={(v) => setOpts((o) => ({ ...o, effort: v as CeoOpts["effort"] }))}>
                <SelectTrigger className="h-8"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="auto">Auto</SelectItem>
                  <SelectItem value="quick">Quick · 15 turns</SelectItem>
                  <SelectItem value="standard">Standard · 40</SelectItem>
                  <SelectItem value="deep">Deep · 80</SelectItem>
                </SelectContent>
              </Select>
            </OptRow>
            <OptRow label="Account">
              <Select value={opts.account} onValueChange={(v) => setOpts((o) => ({ ...o, account: v }))}>
                <SelectTrigger className="h-8"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="auto">Auto · most headroom</SelectItem>
                </SelectContent>
              </Select>
            </OptRow>
            <label className="flex items-center gap-2 text-sm">
              <Checkbox
                checked={!!opts.gate}
                onCheckedChange={(v) => setOpts((o) => ({ ...o, gate: v === true }))}
              />
              Review every role before it runs
            </label>
            <p className="text-xs text-muted-foreground">
              Auto routes per prompt: pure questions get a chat answer; real work runs{" "}
              <b>solo</b> — one Claude Code session with full tools that actually does it, no
              subagents; only genuinely big jobs go to the CEO roster.
            </p>
          </PopoverContent>
        </Popover>
        <Button onClick={send} disabled={busy || !text.trim()} title="Send">
          <Send />
          Send
        </Button>
      </div>
    </div>
  )
}

function OptRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <Label className="text-xs">{label}</Label>
      <div className="w-44">{children}</div>
    </div>
  )
}

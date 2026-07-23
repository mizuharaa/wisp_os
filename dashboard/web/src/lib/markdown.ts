/** Verbatim port of dashboard/index.html's esc()/md() (lines 1661-1691).
 * Safe to feed into dangerouslySetInnerHTML: esc() runs on every text
 * fragment BEFORE any markdown-syntax regex turns it into a tag, so
 * AI-generated content can never inject arbitrary HTML through here.
 * Do not "simplify" this by removing the esc() calls or swapping in a raw
 * find/replace -- that's what would reopen the XSS gap this shape avoids. */

const esc = (s: unknown): string =>
  String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!)

export function renderMarkdown(text: string): string {
  text = String(text || "")
  const blocks: string[] = []
  text = text.replace(/```\w*\n?([\s\S]*?)(?:```|$)/g, (_m, code) => {
    blocks.push(`<pre class="mdcode">${esc(code.replace(/\n$/, ""))}</pre>`)
    return `\x00${blocks.length - 1}\x00`
  })
  const inline = (s: string) =>
    esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, "$1<i>$2</i>")
      .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')

  const lines = text.split(/\r?\n/)
  const out: string[] = []
  let list: { tag: "ul" | "ol"; items: string[] } | null = null
  let para: string[] = []
  let table: string[][] | null = null

  const flushP = () => {
    if (para.length) {
      out.push(`<p>${inline(para.join(" "))}</p>`)
      para = []
    }
  }
  const flushL = () => {
    if (list) {
      out.push(`<${list.tag}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${list.tag}>`)
      list = null
    }
  }
  const flushT = () => {
    if (table) {
      const [head, ...body] = table
      out.push(
        `<table><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr>${body
          .map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`)
          .join("")}</table>`,
      )
      table = null
    }
  }

  for (const raw of lines) {
    const line = raw.trimEnd()
    const bare = line.trim()
    if (!bare) {
      flushP()
      flushL()
      flushT()
      continue
    }
    if (/^\x00\d+\x00$/.test(bare)) {
      flushP()
      flushL()
      flushT()
      out.push(bare)
      continue
    }
    if (/^\|.*\|$/.test(bare)) {
      flushP()
      flushL()
      if (/^\|[\s:|-]+\|$/.test(bare)) continue
      ;(table = table || []).push(bare.slice(1, -1).split("|").map((c) => c.trim()))
      continue
    }
    flushT()
    const h = bare.match(/^(#{1,4})\s+(.*)/)
    const ul = bare.match(/^[-*•]\s+(.*)/)
    const ol = bare.match(/^\d+[.)]\s+(.*)/)
    if (h) {
      flushP()
      flushL()
      out.push(`<h4>${inline(h[2])}</h4>`)
      continue
    }
    if (ul) {
      flushP()
      if (!list || list.tag !== "ul") {
        flushL()
        list = { tag: "ul", items: [] }
      }
      list.items.push(ul[1])
      continue
    }
    if (ol) {
      flushP()
      if (!list || list.tag !== "ol") {
        flushL()
        list = { tag: "ol", items: [] }
      }
      list.items.push(ol[1])
      continue
    }
    if (/^[-=_*]{3,}$/.test(bare)) {
      flushP()
      flushL()
      out.push("<hr>")
      continue
    }
    para.push(bare)
  }
  flushP()
  flushL()
  flushT()
  return out.join("").replace(/\x00(\d+)\x00/g, (_m, i) => blocks[+i] || "")
}

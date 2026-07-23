"""Wisp browser runtime: structured, verified control of a real browser.

Dev tools don't all live locally — GitHub, Figma, dashboards, and every
consumer flow (an Uber Eats order included) live behind a browser session.
Wisp drives a REAL Edge/Chrome over the Chrome DevTools Protocol using a
persistent profile at state/.browser: sign in to a site once and every
later mission acts inside that session. No screenshots on the hot path —
actions run as DOM operations and every one returns observed state
(element found, value after fill, title after navigation), matching the
UIA runtime's verified-readback contract.

Purchases/sends still route through mission approval gates — the runtime
gives capability; the guard decides.

Lazy deps: websocket-client for the CDP socket (501-style UiaError-like
hint without it). Tab listing/opening uses plain HTTP, stdlib only.
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE = os.path.join(ROOT, "state", ".browser")
PORT = int(os.environ.get("WISP_BROWSER_PORT") or 9333)
BASE = "http://127.0.0.1:%d" % PORT

BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


class BrowserError(Exception):
    pass


def _http(path, timeout=5, method="GET"):
    req = urllib.request.Request(BASE + path, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if body else {}


def _alive():
    try:
        return bool(_http("/json/version", timeout=2))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False


def ensure(headless=False):
    """Start (or reuse) the Wisp-profile browser with CDP enabled."""
    if _alive():
        return {"ok": True, "already_running": True, "port": PORT}
    exe = next((p for p in BROWSERS if os.path.exists(p)), None)
    if not exe:
        raise BrowserError("no Edge/Chrome found in standard locations")
    args = [exe, "--remote-debugging-port=%d" % PORT,
            "--user-data-dir=" + PROFILE, "--no-first-run",
            "--no-default-browser-check"]
    if headless:
        args.append("--headless=new")
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        if _alive():
            return {"ok": True, "already_running": False, "port": PORT}
        time.sleep(0.5)
    raise BrowserError("browser did not expose CDP on port %d" % PORT)


def tabs():
    """Open pages: id, title, url — the map for targeting actions."""
    if not _alive():
        raise BrowserError("Wisp browser not running — call ensure/open first")
    return [{"id": t["id"], "title": t.get("title", "")[:120],
             "url": t.get("url", "")[:300]}
            for t in _http("/json") if t.get("type") == "page"]


def open_url(url):
    ensure()
    t = _http("/json/new?" + urllib.parse.quote(str(url), safe=":/?&=%"),
              method="PUT")
    return {"ok": True, "id": t.get("id"), "url": t.get("url")}


def _ws_url(tab_id=None):
    pages = [t for t in _http("/json") if t.get("type") == "page"]
    if not pages:
        raise BrowserError("no open pages")
    if tab_id:
        page = next((t for t in pages if t["id"] == tab_id), None)
        if not page:
            raise BrowserError("no tab with id %r" % tab_id)
    else:
        page = pages[0]
    return page["webSocketDebuggerUrl"]


def _evaluate(tab_id, expression, timeout=25):
    try:
        import websocket
    except ImportError:
        raise BrowserError("websocket-client not installed "
                           "(pip install websocket-client)")
    # suppress_origin: Chrome 111+ rejects CDP sockets that carry an Origin
    ws = websocket.create_connection(_ws_url(tab_id), timeout=timeout,
                                     suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": expression,
                                       "returnByValue": True,
                                       "awaitPromise": True}}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == 1:
                if "error" in m:
                    raise BrowserError(str(m["error"])[:200])
                res = m["result"].get("result", {})
                if m["result"].get("exceptionDetails"):
                    raise BrowserError("page threw: %s" % str(
                        m["result"]["exceptionDetails"].get("exception", {})
                        .get("description", ""))[:200])
                return res.get("value")
    finally:
        ws.close()


def _sel(selector):
    return json.dumps(str(selector))


def act(tab_id=None, action="read", selector=None, value=None, js=None,
        url=None):
    """One structured page action with observed-state readback."""
    if action == "goto":
        if not url:
            raise BrowserError("goto needs url")
        _evaluate(tab_id, "location.href = %s" % json.dumps(str(url)))
        time.sleep(2.5)  # ponytail: fixed settle; swap for load-event wait if flaky
        got = _evaluate(tab_id, "({title: document.title, url: location.href})")
        return {"ok": True, "action": "goto", "after": got}
    if action == "read":
        return {"ok": True, "action": "read", "after": _evaluate(tab_id, (
            "({title: document.title, url: location.href, text: "
            "document.body ? document.body.innerText.slice(0, 6000) : ''})"))}
    if action == "click":
        if not selector:
            raise BrowserError("click needs selector")
        out = _evaluate(tab_id, (
            "(() => { const el = document.querySelector(%s);"
            " if (!el) return {found: false};"
            " el.click(); return {found: true, text: (el.innerText||'')"
            ".slice(0,200)}; })()") % _sel(selector))
        return {"ok": True, "action": "click", "verified": bool(out.get("found")),
                "after": out}
    if action == "fill":
        if not selector:
            raise BrowserError("fill needs selector")
        out = _evaluate(tab_id, (
            "(() => { const el = document.querySelector(%s);"
            " if (!el) return {found: false};"
            " const set = Object.getOwnPropertyDescriptor("
            "  Object.getPrototypeOf(el), 'value');"
            " if (set && set.set) set.set.call(el, %s); else el.value = %s;"
            " el.dispatchEvent(new Event('input', {bubbles: true}));"
            " el.dispatchEvent(new Event('change', {bubbles: true}));"
            " return {found: true, value: el.value}; })()")
            % (_sel(selector), json.dumps(str(value or "")),
               json.dumps(str(value or ""))))
        return {"ok": True, "action": "fill",
                "verified": bool(out.get("found")) and
                out.get("value") == str(value or ""),
                "after": out}
    if action == "eval":
        if not js:
            raise BrowserError("eval needs js")
        return {"ok": True, "action": "eval",
                "after": _evaluate(tab_id, str(js))}
    raise BrowserError("unknown action %r" % action)

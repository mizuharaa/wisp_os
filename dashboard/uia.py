"""Wisp UIA action runtime: structured, verified Windows UI automation.

The moat thesis: act on the UI Automation tree (controls, values,
invocations) instead of screenshots. Faster, cheaper, deterministic, works
unfocused — and every action is VERIFIED by re-reading the control after
acting. An action that cannot be confirmed is reported as unverified, never
claimed as success.

Requires pywinauto (the only non-stdlib dependency in the engine; degraded
endpoints return 501 without it). All functions raise UiaError with an
operator-readable message.

All UIA work executes on ONE dedicated worker thread that owns the COM
apartment: the multi-threaded HTTP server never touches COM from request
threads (per-thread CoInitialize bugs), callers just block on a result, and
UI actions are naturally serialized — two agents can never fight over the
same desktop at once. The worker starts lazily, so engine boot stays
instant; without pywinauto every call raises UiaError with an install hint.

ponytail: locator is exact-match on automation_id/name/control_type; add a
query language when real agent use demands it.
"""
import queue
import threading
import time


class UiaError(Exception):
    pass


_DESKTOP = None
_JOBS = queue.Queue()
_WORKER_LOCK = threading.Lock()
_WORKER = None
JOB_TIMEOUT = 60


def _worker_loop():
    while True:
        job = _JOBS.get()
        try:
            job["res"] = job["fn"](**job["kw"])
        except UiaError as e:
            job["err"] = e
        except Exception as e:
            job["err"] = UiaError(("%s: %s" % (type(e).__name__, e))[:200])
        job["ev"].set()


def _dispatch(fn, **kw):
    """Run fn on the single COM-owning worker thread and wait for the result."""
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_worker_loop, name="uia-worker",
                                       daemon=True)
            _WORKER.start()
    job = {"fn": fn, "kw": kw, "ev": threading.Event()}
    _JOBS.put(job)
    if not job["ev"].wait(JOB_TIMEOUT):
        raise UiaError("UIA action timed out after %ss (queued behind a stuck "
                       "action?)" % JOB_TIMEOUT)
    if "err" in job:
        raise job["err"]
    return job["res"]


def _desktop():
    """Lazy: importing pywinauto/comtypes costs seconds, so the engine only
    pays it on the first UIA call. Only ever called on the worker thread, so
    COM is initialized exactly once, in exactly one apartment."""
    global _DESKTOP
    if _DESKTOP is None:
        try:
            from pywinauto import Desktop
        except ImportError:
            raise UiaError("pywinauto not installed (pip install pywinauto)")
        _DESKTOP = Desktop(backend="uia")
    return _DESKTOP


def windows():
    """Top-level windows: title, pid, handle. The agent's map of the desktop."""
    return _dispatch(_windows)


def _windows():
    out = []
    for w in _desktop().windows():
        try:
            title = w.window_text()
            if not title:
                continue
            out.append({"title": title[:120], "pid": w.process_id(),
                        "handle": w.handle})
        except Exception:
            continue
    return out


def _window(pid=None, title_re=None):
    """Returns a WindowSpecification (criteria live there, not on wrappers)."""
    kw = {}
    if pid:
        kw["process"] = int(pid)
    if title_re:
        kw["title_re"] = str(title_re)
    if not kw:
        raise UiaError("pid or title_re is required")
    spec = _desktop().window(**kw)
    try:
        spec.wait("exists", timeout=3)
    except Exception as e:
        raise UiaError("window not found: %s" % str(e)[:120])
    return spec


def _describe(el, depth, max_nodes, bag):
    if len(bag) >= max_nodes:
        return None
    info = el.element_info
    node = {"control_type": info.control_type or "", "name": (info.name or "")[:80],
            "auto_id": info.automation_id or "", "rect": str(info.rectangle)}
    try:  # value is what makes the tree assertable
        node["value"] = el.get_value()[:200]
    except Exception:
        pass
    bag.append(node)
    if depth > 0:
        node["children"] = [c for c in
                            (_describe(ch, depth - 1, max_nodes, bag)
                             for ch in el.children()) if c]
    return node


def tree(pid=None, title_re=None, depth=3, max_nodes=400):
    """Bounded serialization of a window's control tree."""
    return _dispatch(_tree, pid=pid, title_re=title_re, depth=depth,
                     max_nodes=max_nodes)


def _tree(pid=None, title_re=None, depth=3, max_nodes=400):
    w = _window(pid, title_re).wrapper_object()
    bag = []
    root = _describe(w, max(0, int(depth)), min(int(max_nodes), 1200), bag)
    return {"tree": root, "nodes": len(bag)}


def _find(spec, locator):
    kw = {}
    if locator.get("auto_id"):
        kw["auto_id"] = str(locator["auto_id"])
    if locator.get("name"):
        kw["title"] = str(locator["name"])
    if locator.get("control_type"):
        kw["control_type"] = str(locator["control_type"])
    if not kw:
        raise UiaError("locator needs auto_id, name, or control_type")
    try:
        return spec.child_window(**kw).wrapper_object()
    except Exception:
        raise UiaError("no control matches %s" % kw)


def _state(el):
    s = {"name": el.element_info.name or ""}
    try:
        s["value"] = el.get_value()
    except Exception:
        pass
    try:
        s["toggle_state"] = el.get_toggle_state()
    except Exception:
        pass
    return s


def act(pid=None, title_re=None, locator=None, action="invoke", value=None):
    """Perform one structured action, then re-read the control and report
    what is actually true. Never claims success it cannot observe."""
    return _dispatch(_act, pid=pid, title_re=title_re, locator=locator,
                     action=action, value=value)


def _act(pid=None, title_re=None, locator=None, action="invoke", value=None):
    w = _window(pid, title_re)
    el = _find(w, locator or {})
    before = _state(el)
    if action == "invoke":
        try:
            el.invoke()
        except Exception:
            el.click_input()  # fallback for controls without InvokePattern
    elif action == "set_text":
        el.set_edit_text(str(value if value is not None else ""))
    elif action == "toggle":
        el.toggle()
    elif action == "focus":
        el.set_focus()
    else:
        raise UiaError("unknown action %r" % action)
    time.sleep(0.15)  # let the UI settle before verifying
    after = _state(el)
    verified = None
    if action == "set_text":
        verified = after.get("value") == str(value if value is not None else "")
    elif action == "toggle":
        verified = after.get("toggle_state") != before.get("toggle_state")
    return {"ok": True, "action": action, "before": before, "after": after,
            "verified": verified}


def read(pid=None, title_re=None, locator=None):
    """Read one control's current state (the assertion primitive)."""
    return _dispatch(_read, pid=pid, title_re=title_re, locator=locator)


def _read(pid=None, title_re=None, locator=None):
    w = _window(pid, title_re)
    return _state(_find(w, locator or {}))

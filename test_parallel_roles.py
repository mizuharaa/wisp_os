"""Self-check: independent roles run as a parallel wave; dependencies still
gate. Patches the worker/verifier/account plumbing so no model, account, or
subprocess is touched — this tests the scheduler, nothing else."""
import sys
import threading
import time
import uuid

sys.path.insert(0, "dashboard")
sys.path.insert(0, "memory")
import ceo


def main():
    events = []          # (role_id, "start"|"end", t)
    gauge = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def fake_worker(cid, role, ctx, cfg_dir, **kw):
        with lock:
            gauge["now"] += 1
            gauge["peak"] = max(gauge["peak"], gauge["now"])
            events.append((role["id"], "start", time.time()))
        time.sleep(0.4)
        with lock:
            gauge["now"] -= 1
            events.append((role["id"], "end", time.time()))
        return {"result": "completed the assignment with concrete evidence",
                "is_error": False, "subtype": "", "session_id": "s-" + role["id"],
                "total_cost_usd": 0}

    ceo._worker = fake_worker
    ceo._verify_role = lambda role: ("accept", "")
    ceo.emit = lambda cid, detail, event="ceo": None
    ceo.pulse.least_used = lambda: ""
    ceo.pulse.dir_for = lambda a: ""
    ceo._recall = lambda text: ""

    cid = "partest" + uuid.uuid4().hex[:8]
    mk = lambda rid, deps: {
        "id": rid, "title": rid, "mission": "do " + rid, "model": "haiku",
        "turns": 5, "depends_on": deps, "review": False, "status": "pending",
        "result": "", "detail": "", "next_action": "", "provider": "claude",
    }
    o = {"cid": cid, "name": "parallel self-check", "mission": "check waves",
         "status": "planning", "route": "plan", "roles":
         [mk("a", []), mk("b", []), mk("c", ["a"]), mk("d", [])],
         "workdir": ".", "account_pref": "none", "detail": "", "next_action": ""}
    ceo._save(o)
    import os
    archived = os.path.join(os.path.dirname(ceo._path(cid)), "archive", cid + ".json")
    try:
        ceo._run(cid)
        # successful missions auto-archive out of the active dir
        where = ceo._path(cid) if os.path.exists(ceo._path(cid)) else archived
        final = ceo._load_json(where)
        statuses = {r["id"]: r["status"] for r in final["roles"]}
        assert all(s == "done" for s in statuses.values()), statuses
        assert final["status"] == "done", final["status"]
        # a, b, d are independent -> they must actually overlap
        assert gauge["peak"] >= 2, "no parallelism observed (peak=%d)" % gauge["peak"]
        # c depends on a -> c must start after a ended
        t = {(rid, kind): ts for rid, kind, ts in events}
        assert t[("c", "start")] >= t[("a", "end")], "dependency order violated"
        print("peak concurrency:", gauge["peak"])
        print("PARALLEL_ROLES_OK")
    finally:
        for p in (ceo._path(cid), archived):
            try:
                os.remove(p)
            except OSError:
                pass


if __name__ == "__main__":
    main()

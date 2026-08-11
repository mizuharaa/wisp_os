#!/usr/bin/env python3
"""Pulse: the outside-world strip for the dashboard (stdlib only).

Reads gitignored state/pulse.json for credentials (values copied from the
owner's env — never paths, never committed) and serves one cached snapshot:

  claude   token usage + countdown to the 5h-window reset, per account
           (approximation: window starts at the first message seen in the
           last 5h across that account's transcript files)
  github   recent commits via the events API
  gmail    unread count + latest subjects via IMAP (app password)
  spotify  now playing via refresh-token flow; "not connected" until
           client_id/client_secret/refresh_token exist in pulse.json
  outlook  calendar via Microsoft Graph (device-code auth, no client secret).
           Sign in once: python dashboard/pulse.py --outlook-login

A daemon thread refreshes every 45s so requests never block on the network.
Each service degrades independently; calendar/mail/GitHub keep last-good data
with explicit stale/error metadata when a transient refresh fails.
"""
import base64
import contextlib
import datetime
import email.header
import imaplib
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "state", "pulse.json")
# last good snapshot on disk. The live SNAP is memory-only, so anything built
# from it (the daily briefing's GitHub section) had nothing to read when the
# network — or the process — was gone. This is the offline copy.
CACHE = os.path.join(ROOT, "state", "pulse-cache.json")
STATE_WRITE_LOCK = os.path.join(ROOT, "state", "pulse-state.lock")
WINDOW = 5 * 3600
CACHED_SERVICES = ("github", "gmail", "outlook")
CACHE_STALE_AFTER = 3600
LOCK = threading.Lock()


@contextlib.contextmanager
def _state_write_lock(path=STATE_WRITE_LOCK, timeout=5, stale_after=30):
    """Serialize read/merge/replace across dashboard and login processes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    deadline = time.monotonic() + timeout
    acquired = False
    while not acquired:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, ("%d %d\n" % (os.getpid(), threading.get_ident())).encode())
            except BaseException:
                os.close(fd)
                try:
                    os.remove(path)
                except OSError:
                    pass
                raise
            else:
                os.close(fd)
            acquired = True
        except FileExistsError:
            try:
                stale = time.time() - os.path.getmtime(path) > stale_after
            except OSError:
                stale = False
            if stale:
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for pulse state lock")
            time.sleep(0.02)
    try:
        yield
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _atomic_json_write(path, value, indent=None):
    """Atomically write JSON without sharing a predictable .tmp filename.

    Several dashboard server processes can overlap during a self-restart. A
    fixed ``path + '.tmp'`` lets one process replace/delete another process's
    temporary file. The unique name plus ``os.replace`` keeps readers on either
    complete version and makes concurrent writers safe (last complete write
    wins).
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%s.%s.%s.tmp" % (path, os.getpid(), threading.get_ident(), time.time_ns())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        last_error = None
        for delay in (0, .03, .12):
            if delay:
                time.sleep(delay)
            try:
                os.replace(tmp, path)
                return
            except OSError as e:
                last_error = e
        raise last_error
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _stamp(value, fallback=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(fallback or 0))


def _cache_clean(value):
    """Remove runtime-only failure flags from a last-good service payload."""
    out = dict(value) if isinstance(value, dict) else {}
    for key in ("cached", "stale", "refresh_error", "refresh_attempt_asof"):
        out.pop(key, None)
    return out


def _service_good(value):
    return (isinstance(value, dict) and bool(value)
            and not value.get("error") and not value.get("refresh_error"))


def _normalise_cache(raw):
    """Read both the legacy global-asof schema and the per-service schema."""
    raw = raw if isinstance(raw, dict) else {}
    global_asof = _stamp(raw.get("asof"))
    out = {"asof": global_asof}
    for key in CACHED_SERVICES:
        value = raw.get(key)
        if not _service_good(value):
            continue
        value = _cache_clean(value)
        value["asof"] = _stamp(value.get("asof"), global_asof)
        out[key] = value
    return out


def _cold_snapshot(raw, now=None):
    """Hydrate the in-memory strip from disk before any network request."""
    now = _stamp(now, time.time())
    cache = _normalise_cache(raw)
    out = {"asof": cache.get("asof", 0)}
    for key in CACHED_SERVICES:
        if key not in cache:
            continue
        value = dict(cache[key])
        value["cached"] = True
        asof = _stamp(value.get("asof"))
        if not asof or now - asof > CACHE_STALE_AFTER:
            value["stale"] = True
        out[key] = value
    return out


# Do not flash an empty calendar/GitHub/Gmail strip while the first refresh is
# in flight. Legacy cache files are upgraded in memory and on their next write.
SNAP = _cold_snapshot(_read_json(CACHE))


def _cfg():
    return _read_json(CFG)


def _http(url, headers=None, data=None, timeout=8):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ---------------------------------------------------------------- claude usage
def _email(base):
    """The logged-in account email, read from <config-dir>/.claude.json (or the
    sibling <dir>.json used by the default ~/.claude). Best-effort, never raises."""
    if not base:
        return None
    for cand in (os.path.join(base, ".claude.json"), base.rstrip("/\\") + ".json"):
        try:
            with open(cand, encoding="utf-8") as f:
                oa = (json.load(f).get("oauthAccount") or {})
            if oa.get("emailAddress"):
                return oa["emailAddress"]
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return None


def _claude_account(acct):
    base = acct.get("dir", "")
    email = acct.get("email") or _email(base)
    d = os.path.join(base, "projects")
    if not os.path.isdir(d):
        return {"name": acct.get("name", "?"), "email": email, "dir": base,
                "error": "no transcript dir"}
    now = time.time()
    # scan ~10.5h back so the CURRENT 5h window can be anchored correctly (the
    # old code took min(ts in last 5h), which slides forward as messages age out
    # so the countdown never actually reaches a reset).
    scan = 2 * WINDOW + 1800
    files = []
    for dirpath, _dirs, fns in os.walk(d):
        for fn in fns:
            if fn.endswith(".jsonl"):
                p = os.path.join(dirpath, fn)
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    continue
                if now - mt < scan:
                    files.append(p)
    events = []  # (ts, tokens_in, tokens_out) for messages within the scan
    for p in files[:80]:
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if '"usage"' not in line or '"timestamp"' not in line:
                        continue
                    try:
                        j = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = j.get("timestamp") or ""
                    try:
                        t = datetime.datetime.fromisoformat(
                            ts.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        continue
                    if now - t > scan or t > now + 300:
                        continue
                    u = (j.get("message") or {}).get("usage") or {}
                    if not u:
                        continue
                    events.append((t,
                                   (u.get("input_tokens") or 0)
                                   + (u.get("cache_creation_input_tokens") or 0),
                                   u.get("output_tokens") or 0))
        except OSError:
            continue
    out = {"name": acct.get("name", "?"), "email": email, "dir": base, "msgs": 0,
           "tokens_in": 0, "tokens_out": 0, "limit_tokens": acct.get("limit_tokens")}
    if not events:
        return out
    events.sort()
    # the 5h window is anchored to its first message and resets exactly 5h later;
    # the next message after that starts a fresh window. Chain the blocks to find
    # the one NOW falls in — its start is fixed, so the countdown is real.
    ws = events[0][0]
    for t, _ti, _to in events:
        if t - ws >= WINDOW:
            ws = t
    if now - ws >= WINDOW:
        return out  # last window elapsed with no new activity -> clear, no reset
    ti = to = msgs = 0
    for t, a, b in events:
        if t >= ws:
            msgs += 1
            ti += a
            to += b
    out.update(msgs=msgs, tokens_in=ti, tokens_out=to, reset_at=int(ws + WINDOW))
    if acct.get("limit_tokens"):
        out["pct"] = min(100, round((ti + to) / acct["limit_tokens"] * 100))
    return out


# --- accurate per-account tracking via the server's own rate-limit headers ----
# Transcripts don't record WHICH account sent each message, so scanning a config
# dir mis-attributes usage when accounts are swapped in one terminal. Instead we
# capture each account's OAuth token as it's seen, then ask Anthropic for that
# account's TRUE unified 5h/7d window (utilization + reset). Server-authoritative,
# per-account, independent of how terminals/dirs are arranged.
SEEN = os.path.join(ROOT, "state", "claude-seen.json")  # gitignored (holds tokens)
RL_CACHE = {}            # account key -> (data, fetched_at)
RL_TTL = 180             # re-probe the server at most every 3 min per account


def _creds(base):
    """{token, expires_ms, email, uuid} for the account currently logged into
    config dir `base` (from its .credentials.json + .claude.json)."""
    try:
        with open(os.path.join(base, ".credentials.json"), encoding="utf-8") as handle:
            oa = (json.load(handle).get("claudeAiOauth") or {})
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not oa.get("accessToken"):
        return {}
    out = {"token": oa["accessToken"], "expires_ms": oa.get("expiresAt")}
    for cand in (os.path.join(base, ".claude.json"), base.rstrip("/\\") + ".json"):
        try:
            with open(cand, encoding="utf-8") as handle:
                a = (json.load(handle).get("oauthAccount") or {})
            if a.get("emailAddress"):
                out["email"], out["uuid"] = a["emailAddress"], a.get("accountUuid")
                break
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return out


def _load_seen():
    try:
        with open(SEEN, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def _capture_seen(cfg):
    """Snapshot every account currently visible (default ~/.claude + configured
    dirs) keyed by accountUuid, so accounts swapped through one terminal
    accumulate and each keeps its own token. Returns the merged seen-map."""
    seen = _load_seen()
    now = int(time.time())
    dirs = {os.path.expanduser("~/.claude")}
    for a in cfg.get("claude_accounts") or []:
        if a.get("dir"):
            dirs.add(a["dir"])
    names = cfg.get("account_names") or {}  # optional {email: friendly name} override
    changed = False
    for d in dirs:
        c = _creds(d)
        uid = c.get("uuid")
        if not (uid and c.get("token")):
            continue
        email = c.get("email")
        rec = seen.get(uid, {"first_seen": now})
        rec.update(email=email, token=c["token"], expires_ms=c.get("expires_ms"),
                   dir=d, last_seen=now,
                   # name by EMAIL, stable when accounts are swapped through one dir
                   name=names.get(email) or (email.split("@")[0] if email else "account"))
        seen[uid] = rec
        changed = True
    if changed:
        try:
            _atomic_json_write(SEEN, seen, indent=1)
        except OSError:
            pass
    return seen


def _ratelimit(token, key):
    """Ask the server for this account's real unified 5h/7d window. Throttled per
    key. Sends a 1-token message (the only call that returns the headers) —
    negligible spend."""
    hit = RL_CACHE.get(key)
    if hit and time.time() - hit[1] < RL_TTL:
        return hit[0]
    body = json.dumps({"model": "claude-haiku-4-5", "max_tokens": 1,
                       "messages": [{"role": "user", "content": "."}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
        headers={"content-type": "application/json", "authorization": "Bearer " + token,
                 "anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            out = _parse_rl(r.headers)
    except urllib.error.HTTPError as e:
        # a 429 (window exhausted) STILL carries the unified reset/utilization
        # headers — read them so an at-limit account shows its real reset.
        out = _parse_rl(e.headers)
        if out.get("reset_at") is None:
            out = {"error": "re-open this account to refresh" if e.code == 401 else "http %d" % e.code}
    except Exception as e:
        out = {"error": type(e).__name__}
    RL_CACHE[key] = (out, time.time())
    return out


def _parse_rl(h):
    def num(k):
        try:
            return float(h.get(k))
        except (TypeError, ValueError):
            return None
    u5, u7 = num("anthropic-ratelimit-unified-5h-utilization"), \
        num("anthropic-ratelimit-unified-7d-utilization")
    return {"reset_at": int(num("anthropic-ratelimit-unified-5h-reset") or 0) or None,
            "reset7d": int(num("anthropic-ratelimit-unified-7d-reset") or 0) or None,
            "pct": None if u5 is None else min(100, round(u5 * 100)),
            "pct7d": None if u7 is None else min(100, round(u7 * 100)),
            "status": h.get("anthropic-ratelimit-unified-5h-status")}


def _claude(cfg):
    seen = _capture_seen(cfg)
    if not seen:  # nothing captured yet -> fall back to transcript scan
        accts = cfg.get("claude_accounts") or []
        return {"accounts": [_claude_account(a) for a in accts]} if accts \
            else {"error": "no Claude account seen yet — open a Claude Code session"}
    out = []
    for uid, r in sorted(seen.items(), key=lambda kv: -(kv[1].get("last_seen") or 0)):
        exp = r.get("expires_ms")
        stale = exp and exp / 1000 < time.time()
        rl = {"error": "re-open this account to refresh"} if stale or not r.get("token") \
            else _ratelimit(r["token"], uid)
        out.append({"name": r.get("name") or "account", "email": r.get("email"),
                    "dir": r.get("dir"), "reset_at": rl.get("reset_at"),
                    "pct": rl.get("pct"), "pct7d": rl.get("pct7d"),
                    "reset7d": rl.get("reset7d"), "error": rl.get("error")})
    return {"accounts": out}


# ---------------------------------------------------------------- github
# The public events API now returns TRIMMED PushEvent payloads (SHAs only, no
# commits[]/size), so counting commits from events yields 0. Source of truth is
# the GraphQL contribution calendar (accurate daily counts + a year of graph
# data); recent commit messages come from the REST commits endpoint of the
# most-recently-pushed repo. Needs a token with read:user scope.
_GQL_CAL = """query($l:String!){user(login:$l){contributionsCollection{
  contributionCalendar{totalContributions weeks{contributionDays{date contributionCount}}}}}}"""


def _github_calendar(user, hdr):
    """Year of daily contribution counts via GraphQL. Returns (days, total) where
    days is a flat [{date, count}] list, or ([], 0) if unavailable (no token)."""
    if "Authorization" not in hdr:
        return [], 0  # GraphQL requires auth; skip cleanly on public-only config
    body = json.dumps({"query": _GQL_CAL, "variables": {"l": user}}).encode()
    d = _http("https://api.github.com/graphql",
              dict(hdr, **{"Content-Type": "application/json"}), body)
    if d.get("errors"):
        raise RuntimeError("GitHub GraphQL returned an error")
    cal = (((d.get("data") or {}).get("user") or {}).get("contributionsCollection")
           or {}).get("contributionCalendar") or {}
    days = [{"date": dd["date"], "count": dd["contributionCount"]}
            for w in cal.get("weeks", []) for dd in w.get("contributionDays", [])]
    return days, cal.get("totalContributions", 0)


def _github(cfg):
    g = cfg.get("github") or {}
    user, token = g.get("user"), g.get("token")
    if not user:
        return {"error": "not connected"}
    hdr = {"User-Agent": "rune-pulse", "Accept": "application/vnd.github+json"}
    if token:
        hdr["Authorization"] = "Bearer " + token
    days, total_year = _github_calendar(user, hdr)
    today_key = datetime.date.today().isoformat()
    today = next((d["count"] for d in reversed(days) if d["date"] == today_key), 0)
    # streak: consecutive days up to and including today with >0 contributions
    streak = 0
    for d in reversed(days):
        if d["date"] > today_key:
            continue
        if d["count"] > 0:
            streak += 1
        elif d["date"] != today_key:  # today at 0 doesn't break a prior streak
            break
    # recent commit messages from the most-recently-pushed repo (events still
    # carry repo + timestamp reliably, just not the commit bodies)
    commits, repos = [], []
    evs = _http("https://api.github.com/users/%s/events?per_page=30" % user, hdr)
    for e in evs:
        if e.get("type") == "PushEvent":
            r = (e.get("repo") or {}).get("name")
            if r and r not in repos:
                repos.append(r)
    commit_fetches = 0
    last_error = None
    for full in repos[:2]:
        try:
            cs = _http("https://api.github.com/repos/%s/commits?per_page=6" % full, hdr)
            commit_fetches += 1
        except Exception as e:
            last_error = e
            continue
        repo = full.split("/")[-1]
        for c in cs:
            cm = c.get("commit") or {}
            commits.append({"repo": repo,
                            "msg": (cm.get("message") or "").split("\n")[0][:80],
                            "ts": (cm.get("author") or {}).get("date", "")})
        if len(commits) >= 8:
            break
    if repos and not commit_fetches and last_error:
        raise last_error
    commits.sort(key=lambda c: c["ts"], reverse=True)
    return {"user": user, "today": today, "year": total_year,
            "streak": streak, "days": days, "commits": commits[:8]}


# ---------------------------------------------------------------- gmail
def _decode(s):
    try:
        return "".join(t.decode(enc or "utf-8", "replace") if isinstance(t, bytes) else t
                       for t, enc in email.header.decode_header(s))
    except Exception:
        return s


def _gmail(cfg):
    g = cfg.get("gmail") or {}
    addr, pw = g.get("email"), (g.get("app_password") or "").replace(" ", "")
    if not addr or not pw:
        return {"error": "not connected"}
    m = imaplib.IMAP4_SSL("imap.gmail.com", timeout=8)
    try:
        m.login(addr, pw)
        m.select("INBOX", readonly=True)
        _typ, data = m.search(None, "UNSEEN")
        ids = (data[0] or b"").split()
        subs = []
        for i in ids[-3:][::-1]:
            _t, md = m.fetch(i, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM)])")
            raw = (md[0][1] if md and md[0] else b"").decode("utf-8", "replace")
            subj = frm = ""
            for ln in raw.splitlines():
                if ln.lower().startswith("subject:"):
                    subj = _decode(ln[8:].strip())
                elif ln.lower().startswith("from:"):
                    frm = _decode(ln[5:].strip()).split("<")[0].strip(' "')
            subs.append({"from": frm[:40], "subject": subj[:80]})
        return {"email": addr, "unread": len(ids), "latest": subs}
    finally:
        try:
            m.logout()
        except Exception:
            pass


# ---------------------------------------------------------------- spotify
def _sp_token(cfg=None):
    """(access_token, error) via the stored refresh token."""
    s = (cfg or _cfg()).get("spotify") or {}
    cid, sec, rt = s.get("client_id"), s.get("client_secret"), s.get("refresh_token")
    if not (cid and sec and rt):
        return None, "not connected"
    auth = base64.b64encode(("%s:%s" % (cid, sec)).encode()).decode()
    tok = _http("https://accounts.spotify.com/api/token",
                {"Authorization": "Basic " + auth,
                 "Content-Type": "application/x-www-form-urlencoded"},
                urllib.parse.urlencode({"grant_type": "refresh_token",
                                        "refresh_token": rt}).encode())
    return tok.get("access_token"), None


def _spotify(cfg):
    at, err = _sp_token(cfg)
    if err:
        return {"error": err}
    req = urllib.request.Request(
        "https://api.spotify.com/v1/me/player/currently-playing",
        headers={"Authorization": "Bearer " + at})
    with urllib.request.urlopen(req, timeout=8) as r:
        if r.status == 204:
            return {"playing": False}
        j = json.load(r)
    item = j.get("item") or {}
    return {"playing": bool(j.get("is_playing")),
            "track": item.get("name", ""),
            "artist": ", ".join(a["name"] for a in item.get("artists", [])),
            "art": ((item.get("album") or {}).get("images") or [{}])[-1].get("url", ""),
            "progress_ms": j.get("progress_ms"),
            "duration_ms": (item.get("duration_ms") or None)}


def spotify_ctl(action, pos_ms=None):
    """Playback control: next / prev / seek / toggle. Needs the
    user-modify-playback-state scope — older tokens get a friendly error."""
    at, err = _sp_token()
    if err:
        return {"error": err}
    base = "https://api.spotify.com/v1/me/player"
    if action == "toggle":
        playing = (get().get("spotify") or {}).get("playing")
        method, url = "PUT", base + ("/pause" if playing else "/play")
    elif action == "next":
        method, url = "POST", base + "/next"
    elif action == "prev":
        method, url = "POST", base + "/previous"
    elif action == "seek":
        method, url = "PUT", base + "/seek?position_ms=%d" % max(0, int(pos_ms or 0))
    else:
        return {"error": "unknown action"}
    req = urllib.request.Request(url, data=b"", method=method,
                                 headers={"Authorization": "Bearer " + at})
    try:
        urllib.request.urlopen(req, timeout=8)
    except urllib.error.HTTPError as e:
        # the token is freshly refreshed, so 401 here = missing scope
        # ("Permissions missing"), not an expired token; 403 similar.
        if e.code in (401, 403):
            return {"error": "controls need permission — hit reconnect on the Spotify card"}
        if e.code == 404:
            return {"error": "no active Spotify device"}
        return {"error": "spotify %d" % e.code}
    except Exception as e:
        return {"error": type(e).__name__}
    # reflect the change immediately instead of waiting for the 7s loop
    try:
        v = _spotify(_cfg())
        with LOCK:
            if SNAP:
                SNAP["spotify"] = v
    except Exception:
        v = {}
    return {"ok": True, "spotify": v}


# -------------------------------------------------- spotify OAuth (code flow)
SPOTIFY_SCOPE = "user-read-currently-playing user-read-playback-state user-modify-playback-state"
# Spotify requires the redirect URI to EXACTLY match one registered in the app,
# and loopback must be the literal 127.0.0.1 (not localhost). Fixed + overridable.
SPOTIFY_REDIRECT_DEFAULT = "http://127.0.0.1:8817/api/spotify/callback"


def spotify_redirect():
    return (_cfg().get("spotify") or {}).get("redirect_uri") or SPOTIFY_REDIRECT_DEFAULT


def spotify_authorize_url(redirect_uri):
    """The consent URL to send the user to, or None if no client_id configured."""
    s = _cfg().get("spotify") or {}
    if not s.get("client_id"):
        return None
    return "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": s["client_id"], "response_type": "code",
        "redirect_uri": redirect_uri, "scope": SPOTIFY_SCOPE})


def spotify_exchange(code, redirect_uri):
    """Exchange an auth code for a refresh token and persist it. Returns an
    error string, or None on success."""
    s = _cfg().get("spotify") or {}
    cid, sec = s.get("client_id"), s.get("client_secret")
    if not (cid and sec):
        return "client_id / client_secret missing in state/pulse.json"
    auth = base64.b64encode(("%s:%s" % (cid, sec)).encode()).decode()
    try:
        tok = _http("https://accounts.spotify.com/api/token",
                    {"Authorization": "Basic " + auth,
                     "Content-Type": "application/x-www-form-urlencoded"},
                    urllib.parse.urlencode({"grant_type": "authorization_code",
                                            "code": code, "redirect_uri": redirect_uri}).encode())
    except urllib.error.HTTPError as e:
        return "token exchange %s: %s" % (e.code, e.read().decode("utf-8", "ignore")[:160])
    except Exception as e:
        return type(e).__name__ + ": " + str(e)[:160]
    rt = tok.get("refresh_token")
    if not rt:
        return "no refresh_token in Spotify response"
    _save_cfg("spotify", {"refresh_token": rt})
    _refresh()  # reflect it on the dashboard immediately
    return None


# ---------------------------------------------------------------- outlook
# Outlook calendar over Microsoft Graph, device-code flow: a PUBLIC client, so
# there is no client secret to keep and no redirect URI to register — you paste
# a code at microsoft.com/devicelogin once and Rune holds a refresh token.
#
#   state/pulse.json:  {"outlook": {"client_id": "<azure app id>",
#                                   "tenant": "common"}}   # or your tenant id
#   then:              python dashboard/pulse.py --outlook-login
#
# Graph ROTATES the refresh token on every use — persist the new one or the next
# refresh fails with invalid_grant (and the calendar silently goes empty).
MS_SCOPE = "offline_access Calendars.Read"
MS_DEVICE = "https://login.microsoftonline.com/%s/oauth2/v2.0/devicecode"
MS_TOKEN = "https://login.microsoftonline.com/%s/oauth2/v2.0/token"
MS_CAL = "https://graph.microsoft.com/v1.0/me/calendarView"
CAL_BEHIND_DAYS = 35
CAL_AHEAD_DAYS = 92
OUTLOOK_REFRESH_SECONDS = 10 * 60
MS_MAX_PAGES = 10
MS_TIMEZONE_DEFAULT = "SE Asia Standard Time"
_MS = {"at": "", "exp": 0}      # access token cache: it lives ~1h, the loop is 45s


def _save_cfg(section, patch):
    with _state_write_lock():
        cfg = _cfg()
        cfg.setdefault(section, {}).update(patch)
        _atomic_json_write(CFG, cfg, indent=2)
    return cfg


def _ms_post(url, form):
    return _http(url, {"Content-Type": "application/x-www-form-urlencoded"},
                 urllib.parse.urlencode(form).encode())


def _ms_token(cfg):
    """(access_token, error) from the stored refresh token, cached until expiry."""
    o = cfg.get("outlook") or {}
    cid, rt = o.get("client_id"), o.get("refresh_token")
    tenant = o.get("tenant") or "common"
    if not cid:
        return None, "not connected — add outlook.client_id to state/pulse.json"
    if not rt:
        return None, "not signed in — run: python dashboard/pulse.py --outlook-login"
    if _MS["at"] and time.time() < _MS["exp"]:
        return _MS["at"], None
    try:
        tok = _ms_post(MS_TOKEN % tenant,
                       {"client_id": cid, "grant_type": "refresh_token",
                        "refresh_token": rt, "scope": MS_SCOPE})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        if "invalid_grant" in body or "AADSTS70008" in body:   # expired/revoked
            return None, "sign-in expired — run: python dashboard/pulse.py --outlook-login"
        return None, "token refresh %s: %s" % (e.code, body[:120])
    if tok.get("refresh_token") and tok["refresh_token"] != rt:
        _save_cfg("outlook", {"refresh_token": tok["refresh_token"]})   # rotated
    at = tok.get("access_token")
    if not at:
        return None, "no access_token from Microsoft"
    _MS.update(at=at, exp=time.time() + max(60, int(tok.get("expires_in") or 3600)) - 120)
    return at, None


def _ms_local(slot, local_tz=None):
    """Convert UTC/offset Graph slots; preferred-local naive slots stay local."""
    raw = (slot or {}).get("dateTime") or ""
    try:
        d = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    local_tz = local_tz or datetime.datetime.now().astimezone().tzinfo
    zone = (slot.get("timeZone") or "UTC").upper()
    if d.tzinfo is not None:
        return d.astimezone(local_tz).replace(tzinfo=None)
    if zone == "UTC":
        return d.replace(tzinfo=datetime.timezone.utc).astimezone(local_tz).replace(tzinfo=None)
    # With Prefer: outlook.timezone Graph intentionally returns a naive wall
    # time in that requested Windows timezone, which is our local timezone.
    return d


def _calendar_bounds(now=None):
    """Return Graph's window with an explicit local UTC offset on both ends."""
    now = now or datetime.datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    start = datetime.datetime.combine(now.date(), datetime.time.min,
                                      tzinfo=now.tzinfo) - datetime.timedelta(
                                          days=CAL_BEHIND_DAYS)
    end = datetime.datetime.combine(now.date(), datetime.time.min,
                                    tzinfo=now.tzinfo) + datetime.timedelta(
                                        days=CAL_AHEAD_DAYS + 1)
    return (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))


def _outlook_timezone(cfg):
    value = str(((cfg.get("outlook") or {}).get("timezone")
                 or MS_TIMEZONE_DEFAULT)).strip()
    # Naive Graph wall times are interpreted as the dashboard's machine-local
    # zone. Keep the supported preference explicit instead of silently showing
    # a different configured Windows zone as Bangkok time.
    if value not in (MS_TIMEZONE_DEFAULT, "UTC") or any(c in value for c in '\r\n"'):
        return MS_TIMEZONE_DEFAULT
    return value


def _safe_graph_next(url):
    """Accept pagination links only from Graph so bearer tokens cannot leak."""
    if not isinstance(url, str) or len(url) > 8192:
        return None
    p = urllib.parse.urlsplit(url)
    if p.scheme.lower() != "https" or (p.hostname or "").lower() != "graph.microsoft.com":
        return None
    return url


def _graph_values(url, headers, fetch=None, max_pages=MS_MAX_PAGES):
    """Follow bounded Graph pagination, rejecting repeated or foreign links."""
    fetch = fetch or _http
    values, seen = [], set()
    next_url = url
    for _page in range(max(1, int(max_pages))):
        if next_url in seen:
            raise ValueError("repeated Microsoft Graph nextLink")
        seen.add(next_url)
        page = fetch(next_url, headers)
        if not isinstance(page, dict):
            raise ValueError("invalid Microsoft Graph response")
        values.extend(page.get("value") or [])
        candidate = page.get("@odata.nextLink")
        if not candidate:
            break
        next_url = _safe_graph_next(candidate)
        if not next_url:
            raise ValueError("unsafe Microsoft Graph nextLink")
    return values


def _outlook_request(cfg, access_token, now=None):
    """Build the timezone-explicit Graph request (kept pure for self-checks)."""
    start, end = _calendar_bounds(now)
    url = MS_CAL + "?" + urllib.parse.urlencode({
        "startDateTime": start, "endDateTime": end,
        "$select": "id,subject,start,end,isAllDay,isCancelled,location,webLink",
        "$orderby": "start/dateTime", "$top": 50})
    headers = {"Authorization": "Bearer " + access_token,
               "Accept": "application/json",
               "Prefer": 'outlook.timezone="%s"' % _outlook_timezone(cfg)}
    return url, headers


def _event_identity(event):
    """Safe optional Graph identity fields used for links and stable UI keys."""
    event_id = str(event.get("id") or "")[:256]
    web_link = str(event.get("webLink") or "")
    if web_link and urllib.parse.urlsplit(web_link).scheme.lower() != "https":
        web_link = ""
    return {"id": event_id, "web_link": web_link[:1200]}


def _outlook(cfg):
    """Calendar events around the current quarter, normalized for every view."""
    at, err = _ms_token(cfg)
    if err:
        return {"error": err}
    url, headers = _outlook_request(cfg, at)
    events = []
    for e in _graph_values(url, headers):
        if e.get("isCancelled"):
            continue
        if e.get("isAllDay"):
            # Graph returns this date in the preferred Outlook timezone. Never
            # convert an all-day midnight again or it can move to an adjacent day.
            day = ((e.get("start") or {}).get("dateTime") or "")[:10]
            if len(day) != 10:
                continue
            end_day = ((e.get("end") or {}).get("dateTime") or "")[:10]
            shaped = {
                "date": day, "time": "all-day", "end_date": end_day or day,
                "end_time": "all-day", "is_all_day": True,
                "summary": (e.get("subject") or "(no subject)")[:100],
                "where": ((e.get("location") or {}).get("displayName") or "")[:60],
            }
            shaped.update(_event_identity(e))
            events.append(shaped)
            continue
        d = _ms_local(e.get("start"))
        if not d:
            continue
        end_d = _ms_local(e.get("end"))
        shaped = {
            "date": d.date().isoformat(),
            "time": d.strftime("%H:%M"),
            "end_date": (end_d or d).date().isoformat(),
            "end_time": (end_d or d).strftime("%H:%M"),
            "is_all_day": False,
            "summary": (e.get("subject") or "(no subject)")[:100],
            "where": ((e.get("location") or {}).get("displayName") or "")[:60],
        }
        shaped.update(_event_identity(e))
        events.append(shaped)
    return {"events": events, "count": len(events)}


def outlook_login():
    """Device-code sign-in. Prints a code, waits for you to enter it, stores the
    refresh token in state/pulse.json. Run once; the loop keeps it alive after."""
    cfg = _cfg()
    o = cfg.get("outlook") or {}
    cid, tenant = o.get("client_id"), (o.get("tenant") or "common")
    if not cid:
        print('Add your Azure app id first:\n  state/pulse.json -> '
              '"outlook": {"client_id": "<app id>", "tenant": "common"}')
        return 1
    d = _ms_post(MS_DEVICE % tenant, {"client_id": cid, "scope": MS_SCOPE})
    print("\n  Go to: %s\n  Enter code: %s\n" % (d.get("verification_uri"), d.get("user_code")))
    print("  waiting for you to approve…")
    deadline = time.time() + int(d.get("expires_in") or 900)
    interval = max(3, int(d.get("interval") or 5))
    while time.time() < deadline:
        time.sleep(interval)
        try:
            tok = _ms_post(MS_TOKEN % tenant,
                           {"client_id": cid, "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                            "device_code": d["device_code"]})
        except urllib.error.HTTPError as e:
            err = json.loads(e.read().decode("utf-8", "ignore") or "{}").get("error", "")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            print("  sign-in failed: " + (err or "unknown"))
            return 1
        if tok.get("refresh_token"):
            _save_cfg("outlook", {"refresh_token": tok["refresh_token"]})
            print("  signed in — Outlook calendar connected.")
            return 0
    print("  timed out — run it again.")
    return 1


# ---------------------------------------------------------------- codex
# OpenAI Codex CLI account. Auth lives in ~/.codex/auth.json (chatgpt OAuth);
# the id_token JWT carries the email + plan. Rate-limit state is read from the
# newest session rollout's token_count events — Codex records a rate_limits
# snapshot there (primary=5h window, secondary=weekly), so no network probe
# is needed; the card shows the last snapshot and how old it is.
def _jwt_claims(tok):
    try:
        seg = tok.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg))
    except Exception:
        return {}


def _codex_dir(cfg):
    return (cfg.get("codex") or {}).get("dir") or os.path.expanduser("~/.codex")


def _codex(cfg):
    base = _codex_dir(cfg)
    auth_path = os.path.join(base, "auth.json")
    try:
        with open(auth_path, encoding="utf-8") as handle:
            auth = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"error": "not connected", "hint": "run `codex login` in a terminal"}
    toks = auth.get("tokens") or {}
    claims = _jwt_claims(toks.get("id_token") or "")
    oai = claims.get("https://api.openai.com/auth") or {}
    out = {"email": claims.get("email"), "mode": auth.get("auth_mode"),
           "plan": oai.get("chatgpt_plan_type"),
           "last_refresh": auth.get("last_refresh")}
    # newest rollout that carries a rate_limits snapshot
    sess = os.path.join(base, "sessions")
    files = []
    for dirpath, _dirs, fns in os.walk(sess):
        for fn in fns:
            if fn.endswith(".jsonl"):
                p = os.path.join(dirpath, fn)
                try:
                    files.append((os.path.getmtime(p), p))
                except OSError:
                    continue
    for _mt, p in sorted(files, reverse=True)[:5]:
        snap = None
        try:
            with open(p, encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if '"rate_limits"' in line:
                        snap = line  # keep the LAST snapshot in the file
        except OSError:
            continue
        if not snap:
            continue
        try:
            j = json.loads(snap)
        except json.JSONDecodeError:
            continue
        pay = j.get("payload") or {}
        rl = pay.get("rate_limits") or {}
        try:
            ts = datetime.datetime.fromisoformat(
                (j.get("timestamp") or "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = _mt
        out["plan"] = rl.get("plan_type") or out["plan"]
        out["asof"] = int(ts)
        cred = rl.get("credits") or {}
        if cred:
            out["credits"] = ("unlimited" if cred.get("unlimited")
                              else "available" if cred.get("has_credits") else "exhausted")
        for key, name in (("primary", ""), ("secondary", "7d")):
            w = rl.get(key)
            if not w:
                continue
            if w.get("used_percent") is not None:
                out["pct" + name] = min(100, round(w["used_percent"]))
            if w.get("resets_in_seconds"):
                out["reset_at" + ("7d" if name else "")] = int(ts + w["resets_in_seconds"])
        break
    return out


# ---------------------------------------------------------------- loop
def _error_text(value):
    if isinstance(value, dict) and value.get("error"):
        return str(value["error"])[:160]
    return ""


def _disconnected(value):
    """Missing configuration is intentional; do not resurrect old account data."""
    error = _error_text(value).lower()
    return "not connected" in error or "not signed in" in error


def _last_good(key, *snapshots):
    for snapshot in snapshots:
        value = (snapshot or {}).get(key)
        cleaned = _cache_clean(value)
        if _service_good(cleaned):
            return cleaned
    return None


def _failed_over(value, fallback, attempted):
    """Return last-good data with an honest refresh failure marker."""
    out = _cache_clean(fallback)
    out["cached"] = True
    out["stale"] = True
    out["refresh_error"] = _error_text(value) or "refresh failed"
    out["refresh_attempt_asof"] = attempted
    return out


def _refresh():
    cfg = _cfg()
    attempted = int(time.time())
    with LOCK:
        previous = dict(SNAP)
    disk = cached()
    snap = {"asof": attempted}
    for key, fn in (("claude", _claude), ("codex", _codex), ("github", _github),
                    ("gmail", _gmail), ("spotify", _spotify), ("outlook", _outlook)):
        reused = False
        prior = _last_good(key, previous, disk) if key in CACHED_SERVICES else None
        # A multi-month calendar window is intentionally richer than the small
        # overview query it replaced. Reuse a fresh last-good Outlook snapshot
        # between ten-minute refreshes instead of re-downloading it every 45s.
        if (key == "outlook" and prior
                and attempted - _stamp(prior.get("asof")) < OUTLOOK_REFRESH_SECONDS):
            value, reused = prior, True
        else:
            try:
                value = fn(cfg)
            except Exception as e:
                value = {"error": type(e).__name__ + ": " + str(e)[:120]}
        if key in CACHED_SERVICES:
            if _service_good(value):
                value = _cache_clean(value)
                value["asof"] = _stamp(value.get("asof"), attempted) if reused else attempted
            elif _error_text(value) and not _disconnected(value):
                fallback = prior
                if fallback:
                    value = _failed_over(value, fallback, attempted)
        snap[key] = value
    with LOCK:
        SNAP.clear()
        SNAP.update(snap)
    _write_cache(snap)
    _record_usage(snap)
    try:
        # keep the subscribed calendar on disk (self-throttled to hourly) so the
        # briefing has your day even with no network. Never fatal to the loop.
        sys.path.insert(0, ROOT)
        import daily_briefing
        daily_briefing.refresh_calendar()
    except Exception:
        pass


def _merge_cache(old, snap):
    """Pure last-good merge used by the writer and the offline self-check."""
    out = _normalise_cache(old)
    for key in CACHED_SERVICES:
        value = snap.get(key)
        if _service_good(value):
            value = _cache_clean(value)
            value["asof"] = _stamp(value.get("asof"), snap.get("asof"))
            previous_asof = _stamp((out.get(key) or {}).get("asof"))
            if value["asof"] >= previous_asof:
                out[key] = value
        elif _disconnected(value):
            out.pop(key, None)
    service_stamps = [_stamp(out[key].get("asof")) for key in CACHED_SERVICES
                      if key in out]
    out["asof"] = max(service_stamps) if service_stamps else 0
    return out


def _write_cache(snap):
    """Persist the snapshot so the briefing can read GitHub activity with no
    network (and before the first refresh of a cold start). Best-effort: a failed
    write must never take the pulse loop down."""
    try:
        with _state_write_lock():
            # Re-read only after acquiring the cross-process lock. Otherwise a
            # slower stale writer can erase another service's newer snapshot.
            old = _read_json(CACHE)
            merged = _merge_cache(old, snap)
            if len(merged) < 2 and len(_normalise_cache(old)) < 2:
                return
            _atomic_json_write(CACHE, merged)
    except OSError:
        pass


def cached():
    """Last good snapshot from disk — works offline, works before first refresh."""
    return _normalise_cache(_read_json(CACHE))


# ------------------------------------------------------- recorded history
# pulse only ever knew "now": every refresh overwrote the last snapshot, so
# nothing could draw usage over time or activity by hour. Two small recorders
# fix that — an append-only utilisation log, and an incremental scan of the
# CLI transcripts into absolute hour buckets.
USAGE_LOG = os.path.join(ROOT, "state", "usage.jsonl")
USAGE_GAP = 240          # min seconds between rows (the loop itself runs at 45s)
USAGE_KEEP = 48 * 3600   # rolling window kept on disk
ACTIVITY = os.path.join(ROOT, "state", "activity-grid.json")
ACTIVITY_KEEP = 8 * 86400


def _usage_rows():
    try:
        with open(USAGE_LOG, encoding="utf-8") as handle:
            rows = []
            for line in handle:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return rows
    except OSError:
        return []


def _record_usage(snap):
    """Append one {ts, <connector>: pct} row. Claude reports per account, so the
    band is the account nearest its ceiling — the one that actually gates work."""
    row = {"ts": int(time.time())}
    pcts = [a.get("pct") for a in (snap.get("claude") or {}).get("accounts") or []
            if isinstance(a.get("pct"), (int, float))]
    if pcts:
        row["claude"] = max(pcts)
    codex_pct = (snap.get("codex") or {}).get("pct")
    if isinstance(codex_pct, (int, float)):
        row["codex"] = codex_pct
    if len(row) == 1:
        return  # nothing measurable this cycle; don't write an empty sample
    rows = _usage_rows()
    if rows and row["ts"] - (rows[-1].get("ts") or 0) < USAGE_GAP:
        return
    rows.append(row)
    cut = row["ts"] - USAGE_KEEP
    kept = [r for r in rows if (r.get("ts") or 0) >= cut]
    try:
        if len(kept) != len(rows):   # trim by rewrite, else cheap append
            with open(USAGE_LOG, "w", encoding="utf-8") as handle:
                handle.writelines(json.dumps(r) + "\n" for r in kept)
        else:
            with open(USAGE_LOG, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row) + "\n")
    except OSError:
        pass


def usage_series(hours=12):
    """{keys, rows} of recorded utilisation inside the window."""
    cut = time.time() - hours * 3600
    rows = [r for r in _usage_rows() if (r.get("ts") or 0) >= cut]
    keys = sorted({k for r in rows for k in r if k != "ts"})
    return {"keys": keys, "rows": rows, "window_hours": hours}


def _transcript_dirs():
    cfg = _cfg()
    dirs = [os.path.expanduser("~/.claude/projects")]
    for a in cfg.get("claude_accounts") or []:
        if a.get("dir"):
            dirs.append(os.path.join(a["dir"], "projects"))
    dirs.append(os.path.join(_codex_dir(cfg), "sessions"))
    return dirs


def _scan_activity():
    """Fold CLI transcript timestamps into absolute hour buckets, reading only
    the bytes appended since the last pass. The first pass reads the whole 7-day
    tail (tens of MB, seconds); every pass after it is near-free."""
    doc = _read_json(ACTIVITY) or {}
    files = doc.get("files") or {}
    buckets = doc.get("buckets") or {}
    now = time.time()
    cut = now - ACTIVITY_KEEP
    for base in _transcript_dirs():
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, fns in os.walk(base):
            for fn in fns:
                if not fn.endswith(".jsonl"):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    size = os.path.getsize(path)
                    if os.path.getmtime(path) < cut:
                        files.pop(path, None)
                        continue
                except OSError:
                    continue
                start = files.get(path, 0)
                if start > size:
                    start = 0  # file was rotated/truncated -> re-read it
                if start == size:
                    continue
                try:
                    with open(path, encoding="utf-8", errors="ignore") as handle:
                        handle.seek(start)
                        for line in handle:
                            hour = _line_hour(line)
                            if hour:
                                buckets[hour] = buckets.get(hour, 0) + 1
                except OSError:
                    continue
                files[path] = size
    keep = int(cut // 3600)
    buckets = {h: c for h, c in buckets.items() if int(h) >= keep}
    try:
        _atomic_json_write(ACTIVITY, {"files": files, "buckets": buckets,
                                      "asof": int(now)})
    except OSError:
        pass


def _line_hour(line):
    """Absolute epoch-hour of a transcript line, as a string key. Cheap: bails
    on the substring test before any JSON parsing."""
    i = line.find('"timestamp"')
    if i < 0:
        return None
    j = line.find('"', line.find(":", i) + 1)
    if j < 0:
        return None
    raw = line[j + 1:line.find('"', j + 1)]
    try:
        t = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.astimezone()
    return str(int(t.timestamp() // 3600))


def activity_grid():
    """7x24 local-time counts for the last 7 days: grid[weekday][hour], Mon=0."""
    doc = _read_json(ACTIVITY) or {}
    grid = [[0] * 24 for _ in range(7)]
    cut = time.time() - 7 * 86400
    total = 0
    for hour, count in (doc.get("buckets") or {}).items():
        try:
            stamp = int(hour) * 3600
        except ValueError:
            continue
        if stamp < cut:
            continue
        local = datetime.datetime.fromtimestamp(stamp)
        grid[local.weekday()][local.hour] += count
        total += count
    return {"grid": grid, "total": total, "asof": doc.get("asof") or 0,
            "source": "claude + codex transcripts"}


def _loop():
    while True:
        try:
            _refresh()
            _scan_activity()
        except Exception as e:
            # a crash here used to kill the thread outright: the strip froze on
            # its last snapshot forever and said nothing. Log and keep looping.
            with LOCK:
                SNAP["loop_error"] = type(e).__name__ + ": " + str(e)[:120]
        time.sleep(45)


def _loop_spotify():
    """Now-playing changes every few minutes — refresh it on its own fast cadence
    so the card feels live, instead of waiting up to 45s for the full loop."""
    while True:
        time.sleep(7)
        try:
            v = _spotify(_cfg())
        except Exception as e:
            v = {"error": type(e).__name__}
        with LOCK:
            if SNAP:  # don't create a lone-key snapshot before the first full refresh
                SNAP["spotify"] = v


def get():
    with LOCK:
        return dict(SNAP)


# -------------------------------------------------- account routing (spawn/orch)
def accounts():
    return _cfg().get("claude_accounts") or []


def dir_for(name):
    """The CLAUDE_CONFIG_DIR for an account display-name — resolves via the
    seen-cache (email-based names) first, then configured accounts. '' if unknown."""
    for r in _load_seen().values():
        if r.get("name") == name and r.get("dir"):
            return r["dir"]
    for a in accounts():
        if a.get("name") == name:
            return a.get("dir") or ""
    return ""


def least_used():
    """Name of the account with the most headroom — judged on BOTH windows
    (max of 5h and 7d utilization; an account at 0% of 5h but 97% of 7d is
    nearly exhausted, not free). The orchestrator delegates to it."""
    def load(a):
        p5 = a.get("pct") if a.get("pct") is not None else 999
        p7 = a.get("pct7d") if a.get("pct7d") is not None else 0
        return max(p5, p7)
    accs = [a for a in ((get().get("claude") or {}).get("accounts") or [])
            if not a.get("error") and a.get("name")]
    if accs:
        return min(accs, key=load)["name"]
    cfg = accounts()
    return cfg[0]["name"] if cfg else ""


def _selfcheck():
    """Deterministic coverage for cache/Graph helpers; performs no network I/O."""
    plus7 = datetime.timezone(datetime.timedelta(hours=7))
    local_now = datetime.datetime(2026, 7, 14, 12, 0, tzinfo=plus7)
    start, end = _calendar_bounds(local_now)
    assert start == "2026-06-09T00:00:00+07:00", start
    assert end == "2026-10-15T00:00:00+07:00", end
    utc = _ms_local({"dateTime": "2026-07-14T09:30:00.0000000",
                     "timeZone": "UTC"}, plus7)
    assert utc == datetime.datetime(2026, 7, 14, 16, 30), utc
    preferred = _ms_local({"dateTime": "2026-07-14T09:30:00.0000000",
                           "timeZone": MS_TIMEZONE_DEFAULT}, plus7)
    assert preferred == datetime.datetime(2026, 7, 14, 9, 30), preferred
    assert _ms_local({"dateTime": "junk"}, plus7) is None
    assert _outlook_timezone({"outlook": {"timezone": 'bad"\r\n'}}) == MS_TIMEZONE_DEFAULT
    assert _outlook_timezone({"outlook": {"timezone": "Pacific Standard Time"}}) \
        == MS_TIMEZONE_DEFAULT
    request_url, request_headers = _outlook_request(
        {"outlook": {}}, "offline-test-token", local_now)
    request_query = urllib.parse.parse_qs(urllib.parse.urlsplit(request_url).query)
    assert request_query["startDateTime"] == [start]
    assert request_query["endDateTime"] == [end]
    assert request_headers["Prefer"] == 'outlook.timezone="SE Asia Standard Time"'
    assert "webLink" in request_query["$select"][0]
    assert _event_identity({"id": "a", "webLink": "javascript:bad"})["web_link"] == ""

    first = MS_CAL + "?startDateTime=x"
    second = MS_CAL + "?$skiptoken=opaque"
    pages = {
        first: {"value": [{"subject": "one"}], "@odata.nextLink": second},
        second: {"value": [{"subject": "two"}]},
    }
    calls = []

    def fake_fetch(url, _headers):
        calls.append(url)
        return pages[url]

    assert [e["subject"] for e in _graph_values(first, {}, fake_fetch)] == ["one", "two"]
    assert calls == [first, second]
    assert _safe_graph_next("https://graph.microsoft.com/v1.0/me/calendarView?$skip=1")
    assert _safe_graph_next("https://graph.microsoft.com.evil.invalid/steal") is None
    try:
        _graph_values(first, {}, lambda _u, _h: {
            "value": [], "@odata.nextLink": "https://evil.invalid/steal"})
    except ValueError:
        pass
    else:
        raise AssertionError("foreign Graph nextLink accepted")

    old = {"asof": 100,
           "outlook": {"events": [{"summary": "kept"}], "count": 1},
           "github": {"commits": [{"msg": "old"}]},
           "gmail": {"unread": 2}}
    transient = {"asof": 200,
                 "outlook": {"error": "TimeoutError: offline"},
                 "github": {"commits": [{"msg": "new"}], "asof": 200},
                 "gmail": {"error": "not connected"}}
    merged = _merge_cache(old, transient)
    assert merged["outlook"]["events"][0]["summary"] == "kept"
    assert merged["outlook"]["asof"] == 100
    assert merged["github"]["asof"] == 200
    assert "gmail" not in merged
    assert _merge_cache(merged, {"asof": 150,
                                 "github": {"commits": [], "asof": 150}})["github"] \
        == merged["github"]
    stale = _failed_over(transient["outlook"], merged["outlook"], 200)
    assert stale["stale"] and stale["cached"] and stale["asof"] == 100
    cold = _cold_snapshot(merged, now=4000)
    assert cold["outlook"]["stale"] and cold["outlook"]["cached"]

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "cache.json")
        _atomic_json_write(p, merged)
        assert _read_json(p) == merged
        assert not [name for name in os.listdir(d) if name.endswith(".tmp")]
        gate = threading.Barrier(2)
        state_lock = os.path.join(d, "state.lock")

        def merge_key(key):
            gate.wait()
            with _state_write_lock(state_lock):
                value = _read_json(p)
                time.sleep(0.03)
                value[key] = {"asof": 500, "value": key}
                _atomic_json_write(p, value)

        writers = [threading.Thread(target=merge_key, args=(key,))
                   for key in ("outlook2", "github2")]
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join()
        locked = _read_json(p)
        assert "outlook2" in locked and "github2" in locked
        assert not os.path.exists(state_lock)
    # recorded history: line->hour extraction, and hour buckets -> 7x24 grid
    assert _line_hour('{"a":1}') is None
    assert _line_hour('{"type":"user","timestamp":"2026-07-14T09:30:00Z"}') \
        == str(int(datetime.datetime(2026, 7, 14, 9, 30,
                                     tzinfo=datetime.timezone.utc).timestamp() // 3600))
    assert _line_hour('{"timestamp":"not-a-date"}') is None
    marker = datetime.datetime.now().replace(minute=0, second=0, microsecond=0)
    globals()["ACTIVITY"] = os.path.join(tempfile.gettempdir(), "pulse-selfcheck-grid.json")
    _atomic_json_write(ACTIVITY, {"buckets": {
        str(int(marker.timestamp() // 3600)): 5,
        str(int((marker - datetime.timedelta(days=30)).timestamp() // 3600)): 99,
    }})
    live = activity_grid()
    assert live["total"] == 5, live["total"]           # the 30-day-old bucket is dropped
    assert live["grid"][marker.weekday()][marker.hour] == 5
    os.remove(ACTIVITY)
    print("pulse selfcheck: ok (cache merge, state lock, cold start, Graph time/pagination, atomic write, activity grid)")
    return 0


def _start_daemons():
    threading.Thread(target=_loop, daemon=True, name="pulse-refresh").start()
    threading.Thread(target=_loop_spotify, daemon=True, name="pulse-spotify").start()

if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        sys.exit(_selfcheck())
    elif "--outlook-login" in sys.argv:
        sys.exit(outlook_login())
    _refresh()
    print(json.dumps(get(), indent=2)[:3000])
elif os.environ.get("RUNE_PULSE_NO_THREADS") != "1":
    _start_daemons()

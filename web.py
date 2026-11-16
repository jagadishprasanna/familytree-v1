"""Web frontend + JSON API for the family tree (standard library only).

Run:  python web.py [family.txt | gsheet:<id>] [--port 8000] [--host 127.0.0.1]
Then open http://127.0.0.1:8000

Data: with no file argument it uses the Google Sheet named by GOOGLE_SHEET_ID
(credentials in GOOGLE_SERVICE_ACCOUNT_JSON / _FILE), else FAMILY_FILE / family.txt.

Login (recommended for anything public) - set environment variables:
    APP_USERNAME / APP_PASSWORD     one login, or
    APP_USERS='{"alice":"pw1","bob":"pw2"}'   several logins
    SESSION_SECRET                  long random string; keeps logins valid across restarts
    SESSION_DAYS                    session length (default 7)
With none of these set, login is OFF (fine for local use only).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from drive_photos import DriveError, DrivePhotos, parse_folder_id
from family_store import FamilyError, FamilyStore, open_store, to_dict

WEB_DIR = Path(__file__).with_name("web")
store: FamilyStore
CAPSULE: dict = {"client": None, "tried": False}   # Time Capsule: photos from a Google Drive folder


def capsule_client() -> DrivePhotos | None:
    """Created on first use so a Drive problem never stops the family tree from starting."""
    if not CAPSULE["tried"]:
        CAPSULE["client"] = DrivePhotos.from_env()    # a DriveError here is retried on the next request
        CAPSULE["tried"] = True
    return CAPSULE["client"]

# ------------------------------------------------------------------- auth
MAX_BODY = 12 * 1024 * 1024   # largest request body accepted
COOKIE = "ft_session"
AUTH: dict = {"enabled": False, "users": {}, "secret": b"", "ttl": 7 * 86400, "random_secret": False}
MAX_FAILS, FAIL_WINDOW = 5, 15 * 60
_fails: dict[str, list[float]] = {}
_fails_lock = threading.Lock()


def configure_auth(users: dict[str, str] | None, secret: str | None = None, days: float = 7) -> None:
    AUTH["users"] = dict(users or {})
    AUTH["enabled"] = bool(AUTH["users"])
    AUTH["random_secret"] = not secret
    AUTH["secret"] = (secret or secrets.token_urlsafe(48)).encode()
    AUTH["ttl"] = int(days * 86400)
    with _fails_lock:
        _fails.clear()


def load_auth_from_env() -> None:
    users: dict[str, str] = {}
    raw = os.environ.get("APP_USERS", "").strip()
    if raw:
        users.update({str(k): str(v) for k, v in json.loads(raw).items()})
    if os.environ.get("APP_USERNAME") and os.environ.get("APP_PASSWORD"):
        users[os.environ["APP_USERNAME"]] = os.environ["APP_PASSWORD"]
    configure_auth(users, os.environ.get("SESSION_SECRET"), float(os.environ.get("SESSION_DAYS", "7")))


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload: str) -> str:
    return _b64(hmac.new(AUTH["secret"], payload.encode(), hashlib.sha256).digest())


def make_token(user: str, now: float | None = None) -> str:
    payload = _b64(json.dumps({"u": user, "exp": int((now or time.time()) + AUTH["ttl"])}).encode())
    return f"{payload}.{_sign(payload)}"


def verify_token(token: str, now: float | None = None) -> str | None:
    try:
        payload, sig = token.split(".", 1)
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        data = json.loads(_unb64(payload))
        if data["exp"] < (now or time.time()) or data["u"] not in AUTH["users"]:
            return None
        return data["u"]
    except Exception:
        return None


def check_password(user: str, password: str) -> bool:
    expected = AUTH["users"].get(user)
    # compare against a dummy when the user is unknown so timing doesn't reveal usernames
    ok = hmac.compare_digest((expected or "\0dummy").encode(), password.encode())
    return ok and expected is not None


def _gsort(p) -> int:
    return {"M": 0, "F": 1}.get(p.gender, 2)


# ------------------------------------------------------------ tree builders
def pedigree(pid: str, depth: int) -> dict:
    node = {"person": to_dict(store.get(pid)), "parents": []}
    if depth > 0:
        for p in sorted(store.get_parents(pid), key=_gsort):
            node["parents"].append(pedigree(p.id, depth - 1))
    return node


def descendants(pid: str, depth: int) -> dict:
    node = {
        "person": to_dict(store.get(pid)),
        "spouses": [{"person": to_dict(s["person"]), "start": s["start"], "end": s["end"],
                     "current": s["current"]} for s in store.get_spouses(pid)],
        "children": [],
    }
    if depth > 0:
        kids = sorted(store.get_children(pid), key=lambda c: c.birth or "9999")
        node["children"] = [descendants(c.id, depth - 1) for c in kids]
    return node


def person_detail(pid: str) -> dict:
    p = store.get(pid)
    return {
        **to_dict(p),
        "parents": [to_dict(x) for x in store.get_parents(pid)],
        "spouses": [{**to_dict(s["person"]), "start": s["start"], "end": s["end"],
                     "current": s["current"]} for s in store.get_spouses(pid)],
        "children": [to_dict(x) for x in store.get_children(pid)],
        "siblings": [{**to_dict(s["person"]), "type": s["type"]} for s in store.get_siblings(pid)],
    }


# ----------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    server_version = "FamilyTree/1.0"

    def log_message(self, fmt, *args):  # quiet
        pass

    # ---- helpers
    def _send(self, code: int, body: bytes, ctype: str = "application/json",
              headers: list[tuple[str, str]] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if not any(k.lower() == "cache-control" for k, _ in headers or []):
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in headers or []:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200, headers=None) -> None:
        self._send(code, json.dumps(data).encode(), headers=headers)

    def _error(self, msg: str, code: int = 400) -> None:
        self._json({"error": msg}, code)

    def _redirect(self, where: str) -> None:
        self._send(302, b"", "text/plain", [("Location", where)])

    def _page(self, name: str) -> None:
        self._send(200, (WEB_DIR / name).read_bytes(), "text/html; charset=utf-8")

    # ---- auth helpers
    def _user(self) -> str | None:
        if not AUTH["enabled"]:
            return "local"
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            c = SimpleCookie(raw)
        except Exception:
            return None
        return verify_token(c[COOKIE].value) if COOKIE in c else None

    def _client_ip(self) -> str:
        fwd = self.headers.get("X-Forwarded-For")
        return (fwd.split(",")[0].strip() if fwd else self.client_address[0])

    def _cookie(self, value: str, max_age: int) -> list[tuple[str, str]]:
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
        return [("Set-Cookie",
                 f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}")]

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return not origin or urlparse(origin).netloc == self.headers.get("Host")

    def _login(self, data: dict) -> None:
        ip, now = self._client_ip(), time.time()
        with _fails_lock:
            recent = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
            _fails[ip] = recent
            if len(recent) >= MAX_FAILS:
                return self._error("Too many attempts. Try again in a few minutes.", 429)
        user, pw = str(data.get("username", "")).strip(), str(data.get("password", ""))
        if check_password(user, pw):
            with _fails_lock:
                _fails.pop(ip, None)
            return self._json({"ok": True}, headers=self._cookie(make_token(user), AUTH["ttl"]))
        with _fails_lock:
            _fails.setdefault(ip, []).append(now)
        time.sleep(0.4)
        self._error("Incorrect username or password.", 401)

    # ---- routes
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if u.path == "/login":
                return self._redirect("/") if self._user() else self._page("login.html")
            if not self._user():  # not logged in: render nothing
                if u.path.startswith("/api/"):
                    return self._error("not logged in", 401)
                return self._redirect("/login")
            if u.path in ("/", "/index.html"):
                return self._page("index.html")
            if u.path == "/api/status":
                kind = "google-sheet" if store.backend.__class__.__name__ == "SheetBackend" else "text-file"
                return self._json({"source": kind, "location": store.describe(),
                                   "auth": AUTH["enabled"], "user": self._user(),
                                   "max_photo_bytes": store.max_photo_bytes})
            if m := re.fullmatch(r"/api/photo/([^/]+)", u.path):
                got = store.get_photo(m.group(1))
                if not got:
                    return self._error("no photo", 404)
                return self._send(200, got[1], got[0],
                                  [("Cache-Control", "private, max-age=86400")])  # URL carries ?v=<version>
            if u.path == "/api/capsule":
                cap = capsule_client()
                if cap is None:
                    return self._json({"configured": False, "photos": [], "thumbs": False})
                photos = cap.listing(force=q.get("refresh") == "1")
                return self._json({"configured": True, "photos": photos, "thumbs": cap.can_resize,
                                   "skipped": cap.skipped, "seen": cap.seen, "other": cap.other})
            if m := re.fullmatch(r"/api/capsule/photo/([\w-]+)", u.path):
                cap = capsule_client()
                if cap is None:
                    return self._error("Time Capsule is not set up", 404)
                try:
                    mime, data = cap.photo(m.group(1), thumb=q.get("size") == "thumb")
                except KeyError:
                    return self._error("no such photo", 404)
                return self._send(200, data, mime, [("Cache-Control", "private, max-age=86400")])
            if u.path == "/api/people":
                store._load()
                return self._json([to_dict(p) for p in store.people.values()])
            if m := re.fullmatch(r"/api/person/([^/]+)", u.path):
                return self._json(person_detail(m.group(1)))
            if m := re.fullmatch(r"/api/tree/pedigree/([^/]+)", u.path):
                return self._json(pedigree(m.group(1), int(q.get("depth", 4))))
            if m := re.fullmatch(r"/api/tree/descendants/([^/]+)", u.path):
                return self._json(descendants(m.group(1), int(q.get("depth", 5))))
            if u.path == "/api/relationship":
                return self._json({"text": store.relationship(q["a"], q["b"])})
            return self._error("not found", 404)
        except DriveError as e:
            print(f"Time Capsule error: {e}", flush=True)
            return self._error(str(e), 502)
        except (FamilyError, KeyError, ValueError) as e:
            return self._error(str(e))
        except Exception as e:  # never drop the connection (Render would show a 502)
            return self._error(f"server error: {type(e).__name__}: {e}", 500)

    def do_POST(self):
        try:
            if not self._same_origin():
                return self._error("cross-site request blocked", 403)
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._error("request too large", 413)
            raw = self.rfile.read(n)
            path = urlparse(self.path).path
            if path in ("/login", "/logout"):
                data = json.loads(raw or b"{}")
                if path == "/login":
                    return self._login(data)
                return self._json({"ok": True}, headers=self._cookie("", 0))
            if not self._user():
                return self._error("not logged in", 401)
            if m := re.fullmatch(r"/api/photo/([^/]+)", path):          # body = raw image bytes
                return self._json(to_dict(store.set_photo(m.group(1), raw)), 201)
            if m := re.fullmatch(r"/api/person/([^/]+)/delete", path):
                return self._json(store.delete_person(m.group(1)))
            if m := re.fullmatch(r"/api/photo/([^/]+)/delete", path):
                return self._json(to_dict(store.delete_photo(m.group(1))))
            data = json.loads(raw or b"{}")
            if m := re.fullmatch(r"/api/person/([^/]+)", path):
                p = store.update_person(m.group(1), data.get("name"), data.get("gender"),
                                        data.get("birth"), data.get("death"),
                                        data.get("nakshatra"), data.get("gotra"), data.get("ailments"))
                return self._json(to_dict(p))
            if path == "/api/person":
                p = store.add_person(data.get("name", ""), data.get("gender", ""),
                                     data.get("birth", ""), data.get("death", ""),
                                     data.get("id", ""), data.get("nakshatra", ""), data.get("gotra", ""),
                                     data.get("ailments", ""))
                return self._json(to_dict(p), 201)
            if path == "/api/marriage":
                store.add_marriage(data["a"], data["b"], data.get("start", ""), data.get("end", ""))
                return self._json({"ok": True}, 201)
            if path == "/api/parent":
                store.add_parent_child(data["parent_id"], data["child_id"])
                return self._json({"ok": True}, 201)
            return self._error("not found", 404)
        except (FamilyError, KeyError, ValueError, json.JSONDecodeError) as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"server error: {type(e).__name__}: {e}", 500)


def main() -> None:
    global store
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?", default=None)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    store = open_store(a.file)
    load_auth_from_env()
    print(f"Family tree: http://{a.host}:{a.port}  (data: {store.describe()})", flush=True)
    if AUTH["enabled"]:
        print(f"Login: ON ({len(AUTH['users'])} user(s))", flush=True)
        if AUTH["random_secret"]:
            print("Note: SESSION_SECRET not set - everyone is logged out whenever the server restarts.",
                  flush=True)
    else:
        print("WARNING: Login is OFF. Set APP_USERNAME and APP_PASSWORD before exposing this publicly.",
              flush=True)
    folder = os.environ.get("DRIVE_FOLDER_ID", "").strip()
    print("Time Capsule: " + (f"Drive folder {parse_folder_id(folder)}" if folder
                              else "OFF (set DRIVE_FOLDER_ID to turn it on)"), flush=True)
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
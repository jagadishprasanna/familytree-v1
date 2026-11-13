"""Web frontend + JSON API for the family tree (standard library only).

Run:  python web.py [family.txt | gsheet:<id>] [--port 8000] [--host 127.0.0.1]
Then open http://127.0.0.1:8000

With no file argument it uses the Google Sheet named by GOOGLE_SHEET_ID
(credentials in GOOGLE_SERVICE_ACCOUNT_JSON / _FILE), else FAMILY_FILE / family.txt.
It uses the same data as the MCP server, so edits made by Claude and in the
browser show up in each other (reload the page).
"""
from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from family_store import FamilyError, FamilyStore, open_store, to_dict

WEB_DIR = Path(__file__).with_name("web")
store: FamilyStore


def _gsort(p) -> int:
    return {"M": 0, "F": 1}.get(p.gender, 2)


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


class Handler(BaseHTTPRequestHandler):
    server_version = "FamilyTree/1.0"

    def log_message(self, fmt, *args):  # quiet
        pass

    # ---- helpers
    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data).encode())

    def _error(self, msg: str, code: int = 400) -> None:
        self._json({"error": msg}, code)

    # ---- routes
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if u.path == "/api/status":
                kind = "google-sheet" if store.backend.__class__.__name__ == "SheetBackend" else "text-file"
                return self._json({"source": kind, "location": store.describe()})
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
        except (FamilyError, KeyError, ValueError) as e:
            return self._error(str(e))
        except Exception as e:  # never drop the connection (Render would show a 502)
            return self._error(f"server error: {type(e).__name__}: {e}", 500)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
            path = urlparse(self.path).path
            if m := re.fullmatch(r"/api/person/([^/]+)", path):
                p = store.update_person(m.group(1), data.get("name"), data.get("gender"),
                                        data.get("birth"), data.get("death"))
                return self._json(to_dict(p))
            if path == "/api/person":
                p = store.add_person(data.get("name", ""), data.get("gender", ""),
                                     data.get("birth", ""), data.get("death", ""),
                                     data.get("id", ""))
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
    print(f"Family tree: http://{a.host}:{a.port}  (data: {store.describe()})", flush=True)
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
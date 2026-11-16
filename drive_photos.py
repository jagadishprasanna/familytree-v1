"""Time Capsule photos from a Google Drive folder (read-only).

Setup: put the photos in one Drive folder, share that folder with the same service account
that the Google Sheet uses (Viewer is enough), and set DRIVE_FOLDER_ID (the folder id or its URL).
The Google Drive API must be enabled in the Google Cloud project that owns the service account.

Optional environment variables:
    CAPSULE_LIST_SECONDS   how long the folder listing is cached (default 60)
    CAPSULE_CACHE_MB       memory used to cache resized photos (default 48)
    CAPSULE_MAX_PHOTOS     most photos shown (default 500)

Photos are downloaded by the server and resized (needs Pillow; without it originals are sent as-is
and thumbnails are not offered), so the browser never talks to Drive and Drive links are never exposed.
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

API = "https://www.googleapis.com/drive/v3/files"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
SHOWABLE = {"image/jpeg", "image/png", "image/webp", "image/gif"}   # what every browser can display
EXT_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jpe": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp", ".gif": "image/gif"}
FOLDER_MIME = "application/vnd.google-apps.folder"
MAX_FOLDERS = 60                       # subfolders searched (photos inside them are included)


def photo_mime(name: str, mime: str) -> str | None:
    """The image type to show for a Drive file, or None. Trusts the file name too, because files
    uploaded by scripts or sync tools are often labelled application/octet-stream."""
    if mime in SHOWABLE:
        return mime
    if mime.startswith("application/vnd.google-apps."):
        return None
    return EXT_MIME.get(os.path.splitext(name.lower())[1])


def sniff(raw: bytes) -> str | None:
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:4] == b"GIF8":
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None
MAX_DOWNLOAD = 40 * 1024 * 1024
FULL_DIM, THUMB_DIM = 1600, 200

try:                                   # optional: resizing
    from PIL import Image, ImageOps
except ImportError:                    # pragma: no cover
    Image = ImageOps = None


class DriveError(Exception):
    """Something the person can act on (folder not shared, API not enabled...)."""


def parse_folder_id(value: str) -> str:
    m = re.search(r"/folders/([\w-]+)", value) or re.search(r"[?&]id=([\w-]+)", value)
    return (m.group(1) if m else value).strip()


def clean_caption(name: str) -> str:
    stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", name)
    return " ".join(stem.replace("_", " ").replace("-", " ").split())


def _date(item: dict) -> str:
    taken = (item.get("imageMediaMetadata") or {}).get("time", "")      # "2019:07:04 12:00:00"
    m = re.match(r"(\d{4}):(\d\d):(\d\d)", taken)
    if m:
        return "-".join(m.groups())
    return (item.get("modifiedTime") or "")[:10]


class DrivePhotos:
    def __init__(self, session, folder_id: str, email: str = "", list_ttl: float = 60.0,
                 cache_bytes: int = 48 * 1024 * 1024, max_photos: int = 500):
        self.session, self.folder, self.email = session, folder_id, email
        self.list_ttl, self.cache_bytes, self.max_photos = list_ttl, cache_bytes, max_photos
        self.can_resize = Image is not None
        self._lock = threading.RLock()
        self._listing: list[dict] = []
        self._skipped = 0
        self._other: list[str] = []
        self._seen = 0
        self._listed_at = 0.0
        self._cache: OrderedDict[tuple[str, str], tuple[str, bytes]] = OrderedDict()
        self._cached_bytes = 0

    # ---- connection
    @classmethod
    def from_env(cls, folder: str | None = None) -> "DrivePhotos | None":
        folder = parse_folder_id(folder or os.environ.get("DRIVE_FOLDER_ID", ""))
        if not folder:
            return None
        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        file = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
                or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""))
        try:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2.service_account import Credentials
            if raw:
                info = json.loads(raw)
            elif file:
                info = json.loads(Path(file).read_text(encoding="utf-8"))
            else:
                raise DriveError("Set GOOGLE_SERVICE_ACCOUNT_JSON (or _FILE) so the app can read Drive.")
            creds = Credentials.from_service_account_info(info, scopes=[SCOPE])
        except DriveError:
            raise
        except ImportError as e:
            raise DriveError("Install google-auth and requests: pip install gspread") from e
        except Exception as e:
            raise DriveError(f"Could not load Google credentials: {type(e).__name__}") from e
        return cls(AuthorizedSession(creds), folder, info.get("client_email", ""),
                   float(os.environ.get("CAPSULE_LIST_SECONDS", "60")),
                   int(float(os.environ.get("CAPSULE_CACHE_MB", "48")) * 1024 * 1024),
                   int(os.environ.get("CAPSULE_MAX_PHOTOS", "500")))

    def _get(self, url: str, params: dict):
        try:
            r = self.session.get(url, params=params, timeout=30)
        except Exception as e:
            raise DriveError(f"Could not reach Google Drive ({type(e).__name__})") from e
        if r.status_code == 200:
            return r
        try:
            msg = r.json()["error"]["message"]
        except Exception:
            msg = f"HTTP {r.status_code}"
        if r.status_code == 404:
            raise DriveError(f"Drive folder not found. Check DRIVE_FOLDER_ID and share the folder "
                             f"(Viewer) with {self.email or 'the service account'}.")
        if r.status_code == 403 and ("has not been used" in msg or "disabled" in msg):
            raise DriveError("The Google Drive API is not enabled for the service account's Google Cloud "
                             "project. Enable 'Google Drive API' there, wait a minute, and reload.")
        raise DriveError(f"Google Drive said: {msg}")

    # ---- listing
    def listing(self, force: bool = False) -> list[dict]:
        with self._lock:
            if not force and self._listed_at and time.monotonic() - self._listed_at < self.list_ttl:
                return self._listing
            items, skipped, other, seen = [], 0, [], 0
            todo, done = [self.folder], set()
            while todo and len(done) < MAX_FOLDERS:
                fid = todo.pop(0)
                if fid in done:
                    continue
                done.add(fid)
                token = None
                while True:
                    params = {
                        "q": f"'{fid}' in parents and trashed=false",
                        "fields": "nextPageToken,files(id,name,mimeType,description,modifiedTime,"
                                  "imageMediaMetadata(time))",
                        "pageSize": 200, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
                    }
                    if token:
                        params["pageToken"] = token
                    data = self._get(API, params).json()
                    for f in data.get("files", []):
                        name, mime = f.get("name", ""), f.get("mimeType", "")
                        if mime == FOLDER_MIME:
                            todo.append(f["id"])
                            continue
                        seen += 1
                        shown = photo_mime(name, mime)
                        if not shown:
                            if mime.startswith("image/"):
                                skipped += 1            # e.g. HEIC: browsers cannot display it
                            elif len(other) < 8:
                                other.append(f"{name} ({mime or 'unknown type'})")
                            continue
                        items.append({"id": f["id"], "name": name,
                                      "caption": (f.get("description") or "").strip() or clean_caption(name),
                                      "date": _date(f), "version": f.get("modifiedTime", ""), "mime": shown})
                    token = data.get("nextPageToken")
                    if not token:
                        break
            items.sort(key=lambda x: (x["date"] or "9999", x["name"].lower()))
            self._listing, self._skipped = items[:self.max_photos], skipped
            self._other, self._seen = other, seen
            self._listed_at = time.monotonic()
            return self._listing

    @property
    def skipped(self) -> int:
        return self._skipped

    @property
    def other(self) -> list[str]:
        return self._other          # names of files that are not photos (for the 'nothing found' message)

    @property
    def seen(self) -> int:
        return self._seen

    # ---- photos
    def _resize(self, raw: bytes, mime: str) -> dict[str, tuple[str, bytes]]:
        if not self.can_resize or mime == "image/gif":      # keep animated gifs untouched
            return {"full": (mime, raw)}
        try:
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            out = {}
            for key, dim, q in (("full", FULL_DIM, 82), ("thumb", THUMB_DIM, 70)):
                c = im.copy()
                c.thumbnail((dim, dim))
                buf = io.BytesIO()
                c.save(buf, "JPEG", quality=q, optimize=True)
                out[key] = ("image/jpeg", buf.getvalue())
            return out
        except Exception:
            return {"full": (mime, raw)}                    # unreadable by Pillow: pass through

    def photo(self, file_id: str, thumb: bool = False) -> tuple[str, bytes]:
        """(mime, bytes) for a photo in the folder. Only ids from the folder listing are served."""
        with self._lock:
            known = {x["id"]: x for x in self.listing()}
            if file_id not in known:
                known = {x["id"]: x for x in self.listing(force=True)}
            if file_id not in known:
                raise KeyError(file_id)
            ver = known[file_id]["version"]
            kind = "thumb" if thumb and self.can_resize else "full"
            hit = self._cache.get((file_id, f"{ver}:{kind}"))
            if hit:
                self._cache.move_to_end((file_id, f"{ver}:{kind}"))
                return hit
            r = self._get(f"{API}/{file_id}", {"alt": "media", "supportsAllDrives": "true"})
            raw = r.content
            if len(raw) > MAX_DOWNLOAD:
                raise DriveError("That photo is larger than 40 MB.")
            mime = sniff(raw) or known[file_id].get("mime") or "image/jpeg"
            made = self._resize(raw, mime)
            for k, v in made.items():
                self._remember((file_id, f"{ver}:{k}"), v)
            return made.get(kind) or made["full"]

    def _remember(self, key, value) -> None:
        self._cache[key] = value
        self._cached_bytes += len(value[1])
        while self._cached_bytes > self.cache_bytes and len(self._cache) > 1:
            _, (_, b) = self._cache.popitem(last=False)
            self._cached_bytes -= len(b)
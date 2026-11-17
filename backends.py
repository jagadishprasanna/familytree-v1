"""Storage backends for the family tree: a text file or a Google Sheet.

Both expose the same small interface used by FamilyStore:

    read()                      -> list of (kind, fields, where) records
    stale()                     -> True when the data should be re-read
    add_person / add_marriage / add_parent (fields)
    update_person(fields)       -> rewrite the row/line of that person id
    describe()                  -> human readable location

Record kinds: PERSON [id,name,gender,birth,death,nakshatra,gotra,ailments]
              MARRIED [a,b,start,end]
              PARENT [parent_id,child_id]
              PHOTO [person_id,mime,version]   (metadata only; bytes via get_photo)

delete_person(pid)           -> remove the person AND every marriage / parent link / photo that mentions them

Photo methods: set_photo(pid, mime, data) / get_photo(pid) -> (mime, bytes)|None / delete_photo(pid),
and the attribute max_photo_bytes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path

Record = tuple[str, list[str], str]  # (kind, fields, where)


class FamilyError(ValueError):
    """Raised for invalid data or invalid operations."""


FIELDS = {"PERSON": 8, "MARRIED": 4, "PARENT": 2}
EXT_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
MIME_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def pad(fields: list[str], kind: str) -> list[str]:
    n = FIELDS[kind]
    return (list(fields) + [""] * n)[:n]


def records_to_rows(records: list[Record]) -> dict[str, list[list[str]]]:
    rows: dict[str, list[list[str]]] = {k: [] for k in FIELDS}
    for kind, f, where in records:
        if kind == "PHOTO":
            continue  # photos are stored separately (Photos tab / photos folder)
        if kind not in FIELDS:
            raise FamilyError(f"{where}: unknown record type {kind!r}")
        rows[kind].append(pad(f, kind))
    return rows


# =============================================================== text file
class TextBackend:
    """Pipe-delimited text file. See README for the format."""

    HEADER = (
        "# Family tree data\n"
        "# PERSON | id | name | gender | birth | death | nakshatra | gotra | ailments\n"
        "# MARRIED | id | id | start | end\n"
        "# PARENT | parent_id | child_id\n"
    )

    max_photo_bytes = 5_000_000

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._seen = None
        if not self.path.exists():
            self.path.write_text(self.HEADER, encoding="utf-8")

    def describe(self) -> str:
        return str(self.path)

    # photos live next to the data file: family.txt -> family_photos/<id>.jpg
    @property
    def photo_dir(self) -> Path:
        return self.path.parent / f"{self.path.stem}_photos"

    def _photo_files(self) -> list[Path]:
        d = self.photo_dir
        return sorted(p for p in d.iterdir() if p.suffix.lower() in EXT_MIME) if d.is_dir() else []

    def _stamp(self):
        files = []
        for p in self._photo_files():
            try:
                files.append((p.name, p.stat().st_mtime_ns))
            except FileNotFoundError:
                pass
        return (self.path.stat().st_mtime, tuple(files))

    def stale(self) -> bool:
        return self._stamp() != self._seen

    def read(self) -> list[Record]:
        self._seen = self._stamp()
        out: list[Record] = []
        for n, raw in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            f = [x.strip() for x in line.split("|")]
            out.append((f[0].upper(), f[1:], f"line {n}"))
        for p in self._photo_files():
            try:
                ver = f"{p.stat().st_mtime_ns // 1000:x}"
            except FileNotFoundError:
                continue
            out.append(("PHOTO", [p.stem, EXT_MIME[p.suffix.lower()], ver], f"photo file {p.name}"))
        return out

    @staticmethod
    def _safe_id(pid: str) -> None:
        if not re.fullmatch(r"[\w-]+", pid):
            raise FamilyError("photos need a person id made of letters, digits, '-' or '_'")

    def set_photo(self, pid: str, mime: str, data: bytes) -> None:
        self._safe_id(pid)
        d = self.photo_dir
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{pid}.tmp"
        tmp.write_bytes(data)
        self.delete_photo(pid)
        os.replace(tmp, d / f"{pid}{MIME_EXT[mime]}")

    def get_photo(self, pid: str):
        self._safe_id(pid)
        for p in self._photo_files():
            if p.stem == pid:
                return EXT_MIME[p.suffix.lower()], p.read_bytes()
        return None

    def delete_photo(self, pid: str) -> None:
        self._safe_id(pid)
        for p in self._photo_files():
            if p.stem == pid:
                p.unlink()

    def _append(self, line: str) -> None:
        text = self.path.read_text(encoding="utf-8")
        prefix = "" if (not text or text.endswith("\n")) else "\n"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(prefix + line + "\n")

    def add_many(self, kind: str, rows: list[list[str]]) -> None:
        """Append many records in one write."""
        if not rows:
            return
        text = self.path.read_text(encoding="utf-8")
        prefix = "" if (not text or text.endswith("\n")) else "\n"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(prefix + "".join(f"{kind} | " + " | ".join(pad(r, kind)) + "\n" for r in rows))

    def add_person(self, f: list[str]) -> None:
        self._append("PERSON | " + " | ".join(pad(f, "PERSON")))

    def add_marriage(self, f: list[str]) -> None:
        self._append("MARRIED | " + " | ".join(pad(f, "MARRIED")))

    def add_parent(self, f: list[str]) -> None:
        self._append("PARENT | " + " | ".join(pad(f, "PARENT")))

    def update_person(self, f: list[str]) -> None:
        f = pad(f, "PERSON")
        lines = self.path.read_text(encoding="utf-8").splitlines()
        for i, raw in enumerate(lines):
            parts = [x.strip() for x in raw.split("|")]
            if len(parts) > 1 and parts[0].upper() == "PERSON" and parts[1] == f[0]:
                lines[i] = "PERSON | " + " | ".join(f)
                break
        else:
            raise FamilyError(f"could not find line for {f[0]}")
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


    def delete_person(self, pid: str) -> None:
        keep = []
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            parts = [x.strip() for x in raw.split("|")]
            kind = parts[0].upper()
            if kind == "PERSON" and len(parts) > 1 and parts[1] == pid:
                continue
            if kind in ("MARRIED", "PARENT") and pid in parts[1:3]:
                continue
            keep.append(raw)                       # comments, blank lines and everyone else stay
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            self.delete_photo(pid)
        except FamilyError:
            pass                                   # id not usable as a file name: no photo file exists


# ============================================================ Google Sheet
TABS = {
    "PERSON": ("People", ["id", "name", "gender", "birth", "death", "nakshatra", "gotra", "ailments"]),
    "MARRIED": ("Marriages", ["person_a", "person_b", "start", "end"]),
    "PARENT": ("Parents", ["parent_id", "child_id"]),
    "PHOTO": ("Photos", ["person_id", "mime", "version", "data"]),
}
READ_COLS = {"PERSON": 8, "MARRIED": 4, "PARENT": 2, "PHOTO": 3}   # PHOTO: skip the big data column


def _col(n: int) -> str:
    return chr(ord("A") + n - 1)


def parse_sheet_id(value: str) -> str:
    """Accept a bare spreadsheet id or a full docs.google.com URL."""
    m = re.search(r"/spreadsheets/d/([\w-]+)", value)
    return m.group(1) if m else value.strip()


class SheetBackend:
    """Google Sheet with three tabs: People, Marriages, Parents (row 1 = header).

    Reads are cached for `ttl` seconds (Google limits read requests) and
    refreshed immediately after every write made through this backend.
    """

    def __init__(self, spreadsheet, ttl: float = 15.0, client_email: str = "",
                 label: str = "Google Sheet"):
        self.sh = spreadsheet
        self.ttl = ttl
        self.client_email = client_email
        self.label = label
        self._read_at = float("-inf")
        self._ws: dict = {}
        self._photo_cache: dict[str, tuple[str, str, bytes]] = {}   # pid -> (version, mime, bytes)
        self._photo_versions: dict[str, str] = {}
        self._call(self._ensure_schema)

    # --- connection
    @classmethod
    def from_env(cls, sheet: str, ttl: float | None = None) -> "SheetBackend":
        try:
            import gspread
        except ImportError as e:  # pragma: no cover
            raise FamilyError("Install gspread: pip install gspread") from e
        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        file = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
                or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""))
        try:
            if raw:
                info = json.loads(raw)
                gc = gspread.service_account_from_dict(info)
                email = info.get("client_email", "")
            elif file:
                info = json.loads(Path(file).read_text(encoding="utf-8"))
                gc = gspread.service_account(filename=file)
                email = info.get("client_email", "")
            else:
                raise FamilyError(
                    "Set GOOGLE_SERVICE_ACCOUNT_JSON (the key file's contents) or "
                    "GOOGLE_SERVICE_ACCOUNT_FILE (path to the key file).")
        except FamilyError:
            raise
        except Exception as e:
            raise FamilyError(f"Could not load Google credentials: {type(e).__name__}") from e
        sid = parse_sheet_id(sheet)
        try:
            sh = gc.open_by_key(sid)
        except Exception as e:
            raise FamilyError(
                f"Cannot open Google Sheet {sid}: {e}. Share it (Editor) with {email}.") from e
        if ttl is None:
            ttl = float(os.environ.get("SHEET_CACHE_SECONDS", "15"))
        return cls(sh, ttl=ttl, client_email=email, label=f"Google Sheet {sid}")

    # A cell holds at most 50,000 characters; base64 inflates by 4/3, so keep photos small.
    max_photo_bytes = 33_000

    def describe(self) -> str:
        return self.label

    def _call(self, fn, *a, **k):
        try:
            return fn(*a, **k)
        except FamilyError:
            raise
        except Exception as e:
            msg = str(e)
            hint = ""
            if self.client_email and any(x in msg.upper() for x in ("403", "404", "PERMISSION")):
                hint = f" Share the sheet (Editor) with {self.client_email}."
            raise FamilyError(f"Google Sheets error: {msg}.{hint}") from e

    # --- setup
    def _ensure_schema(self) -> None:
        have = {w.title: w for w in self.sh.worksheets()}
        for kind, (title, header) in TABS.items():
            self._ws[kind] = have.get(title) or self.sh.add_worksheet(
                title=title, rows=500, cols=len(header))
        res = self.sh.values_batch_get([f"{t}!1:1" for t, _ in TABS.values()])
        for (kind, (title, header)), vr in zip(TABS.items(), res.get("valueRanges", [])):
            have = [str(c).strip() for c in (vr.get("values") or [[]])[0]]
            # empty tab -> write the header; older/shorter header (e.g. before nakshatra and
            # gotra existed) -> add the missing column names. A custom header is left alone.
            if not have or (have != header and header[:len(have)] == have):
                self._ws[kind].update(range_name="A1", values=[header],
                                      value_input_option="RAW")

    # --- reading
    def stale(self) -> bool:
        return time.monotonic() - self._read_at >= self.ttl

    def read(self) -> list[Record]:
        ranges = [f"{t}!A:{_col(READ_COLS[k])}" for k, (t, h) in TABS.items()]
        res = self._call(self.sh.values_batch_get, ranges)
        out: list[Record] = []
        for (kind, (title, _)), vr in zip(TABS.items(), res.get("valueRanges", [])):
            for i, row in enumerate(vr.get("values", [])):
                if i == 0:
                    continue  # header
                f = [str(c).strip() for c in row]
                if not any(f) or f[0].startswith("#"):
                    continue
                out.append((kind, f, f"{title} row {i + 1}"))
        self._photo_versions = {f[0]: (f + ["", ""])[2] for k, f, _ in out if k == "PHOTO" and f}
        self._read_at = time.monotonic()
        return out

    # --- writing
    def _append(self, kind: str, f: list[str]) -> None:
        self._call(lambda: self._ws[kind].append_row(
            pad(f, kind), value_input_option="RAW", table_range="A1"))

    def add_many(self, kind: str, rows: list[list[str]]) -> None:
        """Append many rows with one API call per 500 rows (a loop of append_row would hit Google's rate limit)."""
        for i in range(0, len(rows), 500):
            chunk = [pad(r, kind) for r in rows[i:i + 500]]
            self._call(lambda c=chunk: self._ws[kind].append_rows(c, value_input_option="RAW", table_range="A1"))

    def add_person(self, f): self._append("PERSON", f)
    def add_marriage(self, f): self._append("MARRIED", f)
    def add_parent(self, f): self._append("PARENT", f)

    def update_person(self, f: list[str]) -> None:
        f = pad(f, "PERSON")
        res = self._call(self.sh.values_batch_get, ["People!A:A"])
        col = res.get("valueRanges", [{}])[0].get("values", [])
        row = next((i + 1 for i, r in enumerate(col) if i > 0 and r and r[0].strip() == f[0]), None)
        if row is None:
            raise FamilyError(f"could not find row for {f[0]}")
        self._call(lambda: self._ws["PERSON"].update(
            range_name=f"A{row}:H{row}", values=[f], value_input_option="RAW"))

    # --- photos (tab "Photos": person_id | mime | version | base64 data)
    def _photo_row(self, pid: str) -> int | None:
        res = self._call(self.sh.values_batch_get, ["Photos!A:A"])
        col = res.get("valueRanges", [{}])[0].get("values", [])
        return next((i + 1 for i, r in enumerate(col) if i > 0 and r and r[0].strip() == pid), None)

    def set_photo(self, pid: str, mime: str, data: bytes) -> None:
        ver = hashlib.sha1(data).hexdigest()[:8]
        row = [pid, mime, ver, base64.b64encode(data).decode()]
        if len(row[3]) > 49_000:
            raise FamilyError("Photo is too large for a Google Sheet cell")
        at = self._photo_row(pid)
        if at:
            self._call(lambda: self._ws["PHOTO"].update(
                range_name=f"A{at}:D{at}", values=[row], value_input_option="RAW"))
        else:
            self._call(lambda: self._ws["PHOTO"].append_row(
                row, value_input_option="RAW", table_range="A1"))
        self._photo_cache[pid] = (ver, mime, data)

    def get_photo(self, pid: str):
        have = self._photo_cache.get(pid)
        if have is None or have[0] != self._photo_versions.get(pid):
            self._load_all_photos()          # one request fetches every photo
            have = self._photo_cache.get(pid)
        return (have[1], have[2]) if have else None

    def _load_all_photos(self) -> None:
        res = self._call(self.sh.values_batch_get, ["Photos!A:D"])
        cache: dict[str, tuple[str, str, bytes]] = {}
        for i, r in enumerate(res.get("valueRanges", [{}])[0].get("values", [])):
            if i == 0 or len(r) < 4 or not r[0].strip():
                continue
            try:
                cache[r[0].strip()] = (r[2].strip(), r[1].strip(), base64.b64decode(r[3]))
            except Exception:
                continue                      # unreadable cell: treat as no photo
        self._photo_cache = cache

    def delete_photo(self, pid: str) -> None:
        at = self._photo_row(pid)
        if at:
            self._call(self._ws["PHOTO"].delete_rows, at)
        self._photo_cache.pop(pid, None)

    def delete_person(self, pid: str) -> None:
        """Delete the person's rows from every tab in ONE batch request (all or nothing)."""
        res = self._call(self.sh.values_batch_get,
                         ["People!A:A", "Marriages!A:B", "Parents!A:B", "Photos!A:A"])
        found = {"PERSON": [], "MARRIED": [], "PARENT": [], "PHOTO": []}
        for kind, vr in zip(("PERSON", "MARRIED", "PARENT", "PHOTO"), res.get("valueRanges", [])):
            for i, row in enumerate(vr.get("values", [])):
                if i > 0 and any(c.strip() == pid for c in row):
                    found[kind].append(i + 1)
        if not found["PERSON"]:
            raise FamilyError(f"could not find row for {pid}")
        requests = [
            {"deleteDimension": {"range": {"sheetId": self._ws[kind].id, "dimension": "ROWS",
                                           "startIndex": r - 1, "endIndex": r}}}
            for kind in ("MARRIED", "PARENT", "PHOTO", "PERSON")
            for r in sorted(found[kind], reverse=True)          # bottom-up so row numbers don't shift
        ]
        try:
            self._call(self.sh.batch_update, {"requests": requests})
        except FamilyError as e:
            if "non-frozen" in str(e):
                raise FamilyError("Google Sheets won't delete every row under a frozen header. "
                                  "In the sheet use View > Freeze > No rows (on the People, Marriages "
                                  "and Parents tabs), then try again.") from e
            raise
        self._photo_cache.pop(pid, None)

    def replace_all(self, rows: dict[str, list[list[str]]]) -> None:
        """Overwrite all three tabs (used by migrate_to_sheet.py)."""
        for kind in ("PERSON", "MARRIED", "PARENT"):      # never touches the Photos tab
            title, header = TABS[kind]
            ws = self._ws[kind]
            self._call(ws.clear)
            self._call(lambda: ws.update(range_name="A1",
                                         values=[header] + rows.get(kind, []),
                                         value_input_option="RAW"))
        self._read_at = float("-inf")
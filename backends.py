"""Storage backends for the family tree: a text file or a Google Sheet.

Both expose the same small interface used by FamilyStore:

    read()                      -> list of (kind, fields, where) records
    stale()                     -> True when the data should be re-read
    add_person / add_marriage / add_parent (fields)
    update_person(fields)       -> rewrite the row/line of that person id
    describe()                  -> human readable location

Record kinds: PERSON [id,name,gender,birth,death]
              MARRIED [a,b,start,end]
              PARENT [parent_id,child_id]
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

Record = tuple[str, list[str], str]  # (kind, fields, where)


class FamilyError(ValueError):
    """Raised for invalid data or invalid operations."""


FIELDS = {"PERSON": 5, "MARRIED": 4, "PARENT": 2}


def pad(fields: list[str], kind: str) -> list[str]:
    n = FIELDS[kind]
    return (list(fields) + [""] * n)[:n]


def records_to_rows(records: list[Record]) -> dict[str, list[list[str]]]:
    rows: dict[str, list[list[str]]] = {k: [] for k in FIELDS}
    for kind, f, where in records:
        if kind not in FIELDS:
            raise FamilyError(f"{where}: unknown record type {kind!r}")
        rows[kind].append(pad(f, kind))
    return rows


# =============================================================== text file
class TextBackend:
    """Pipe-delimited text file. See README for the format."""

    HEADER = (
        "# Family tree data\n"
        "# PERSON | id | name | gender | birth | death\n"
        "# MARRIED | id | id | start | end\n"
        "# PARENT | parent_id | child_id\n"
    )

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._mtime: float | None = None
        if not self.path.exists():
            self.path.write_text(self.HEADER, encoding="utf-8")

    def describe(self) -> str:
        return str(self.path)

    def stale(self) -> bool:
        return self.path.stat().st_mtime != self._mtime

    def read(self) -> list[Record]:
        self._mtime = self.path.stat().st_mtime
        out: list[Record] = []
        for n, raw in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            f = [x.strip() for x in line.split("|")]
            out.append((f[0].upper(), f[1:], f"line {n}"))
        return out

    def _append(self, line: str) -> None:
        text = self.path.read_text(encoding="utf-8")
        prefix = "" if (not text or text.endswith("\n")) else "\n"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(prefix + line + "\n")

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


# ============================================================ Google Sheet
TABS = {
    "PERSON": ("People", ["id", "name", "gender", "birth", "death"]),
    "MARRIED": ("Marriages", ["person_a", "person_b", "start", "end"]),
    "PARENT": ("Parents", ["parent_id", "child_id"]),
}


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
            if not vr.get("values"):
                self._ws[kind].update(range_name="A1", values=[header],
                                      value_input_option="RAW")

    # --- reading
    def stale(self) -> bool:
        return time.monotonic() - self._read_at >= self.ttl

    def read(self) -> list[Record]:
        ranges = [f"{t}!A:{_col(len(h))}" for t, h in TABS.values()]
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
        self._read_at = time.monotonic()
        return out

    # --- writing
    def _append(self, kind: str, f: list[str]) -> None:
        self._call(lambda: self._ws[kind].append_row(
            pad(f, kind), value_input_option="RAW", table_range="A1"))

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
            range_name=f"A{row}:E{row}", values=[f], value_input_option="RAW"))

    def replace_all(self, rows: dict[str, list[list[str]]]) -> None:
        """Overwrite all three tabs (used by migrate_to_sheet.py)."""
        for kind, (title, header) in TABS.items():
            ws = self._ws[kind]
            self._call(ws.clear)
            self._call(lambda: ws.update(range_name="A1",
                                         values=[header] + rows.get(kind, []),
                                         value_input_option="RAW"))
        self._read_at = float("-inf")
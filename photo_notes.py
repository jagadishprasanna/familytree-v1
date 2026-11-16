"""Short descriptions and "who is in this photo" tags for Time Capsule photos.

The photos themselves stay in Google Drive (read-only is fine). Notes are small text records kept next to your family
data, so adding them needs no extra Google sign-in:

  * Google Sheet backend -> a "PhotoNotes" tab (file_id, description, people). It is created on first use.
  * Text-file backend    -> <family file name>.photonotes.json beside the family file.

Each note is keyed by the Drive file id and lists person ids from the tree, which is what relates a photo to people.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from backends import FamilyError

MAX_DESC = 600
MAX_PEOPLE = 30
TITLE, HEADER = "PhotoNotes", ["file_id", "description", "people"]


def clean_desc(text) -> str:
    return " ".join(str(text or "").split())[:MAX_DESC]


class JsonNotes:
    def __init__(self, path):
        self.path, self._lock = Path(path), threading.Lock()

    def all(self) -> dict[str, dict]:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}
            except ValueError as e:
                raise FamilyError(f"{self.path.name} is not valid JSON: {e}") from e
        return {k: {"description": v.get("description", ""), "people": list(v.get("people", []))}
                for k, v in raw.items() if isinstance(v, dict)}

    def set(self, fid: str, description: str, people: list[str]) -> None:
        notes = self.all()
        if description or people:
            notes[fid] = {"description": description, "people": people}
        else:
            notes.pop(fid, None)
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(notes, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)

    def forget(self, fid: str) -> None:
        if fid in self.all():
            self.set(fid, "", [])


class SheetNotes:
    def __init__(self, spreadsheet, ttl: float = 15.0):
        self.sh, self.ttl, self._ws = spreadsheet, ttl, None
        self._rows: list[list[str]] = []
        self._at = float("-inf")
        self._lock = threading.RLock()

    def _sheet(self):
        if self._ws is None:
            have = {w.title: w for w in self.sh.worksheets()}
            self._ws = have.get(TITLE)
            if self._ws is None:
                self._ws = self.sh.add_worksheet(title=TITLE, rows=500, cols=len(HEADER))
                self._ws.update(range_name="A1", values=[HEADER], value_input_option="RAW")
        return self._ws

    def _fetch(self, force: bool = False) -> list[list[str]]:
        with self._lock:
            if force or time.monotonic() - self._at >= self.ttl:
                try:
                    self._sheet()
                    res = self.sh.values_batch_get([f"{TITLE}!A:C"])
                    vr = (res.get("valueRanges") or [{}])[0]
                    self._rows = [[str(c).strip() for c in r] + [""] * (3 - len(r)) for r in vr.get("values", [])]
                except Exception as e:
                    raise FamilyError(f"Photo notes error: {e}") from e
                self._at = time.monotonic()
            return self._rows

    def all(self) -> dict[str, dict]:
        out = {}
        for r in self._fetch()[1:]:
            if r[0]:
                out[r[0]] = {"description": r[1], "people": [x.strip() for x in r[2].split(",") if x.strip()]}
        return out

    def _row_of(self, fid: str) -> int | None:
        for i, r in enumerate(self._fetch(force=True)):
            if i and r[0] == fid:
                return i + 1                                   # 1-based sheet row
        return None

    def set(self, fid: str, description: str, people: list[str]) -> None:
        with self._lock:
            try:
                row, ws = self._row_of(fid), self._sheet()
                if not (description or people):
                    if row:
                        ws.delete_rows(row)
                elif row:
                    ws.update(range_name=f"A{row}:C{row}", values=[[fid, description, ",".join(people)]],
                              value_input_option="RAW")
                else:
                    ws.append_row([fid, description, ",".join(people)], value_input_option="RAW")
            except FamilyError:
                raise
            except Exception as e:
                raise FamilyError(f"Photo notes error: {e}") from e
            self._at = float("-inf")

    def forget(self, fid: str) -> None:
        self.set(fid, "", [])


def open_notes(backend):
    """Notes store that sits beside whichever backend holds the family data."""
    if hasattr(backend, "sh"):
        return SheetNotes(backend.sh)
    return JsonNotes(Path(backend.path).with_suffix(".photonotes.json"))
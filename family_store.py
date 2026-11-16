"""Family tree store: in-memory graph on top of a pluggable backend.

Backends (see backends.py): a pipe-delimited text file or a Google Sheet.
The data is re-read when it changes (text: file mtime; sheet: short cache),
so hand edits made in the file/sheet show up without restarting.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from threading import RLock

from backends import FamilyError, SheetBackend, TextBackend  # noqa: F401  (re-exported)

DEFAULT_FILE = Path(__file__).with_name("family.txt")


@dataclass
class Person:
    id: str
    name: str
    gender: str = ""
    birth: str = ""
    death: str = ""
    nakshatra: str = ""   # birth star (e.g. Rohini)
    gotra: str = ""       # lineage (e.g. Bharadwaja)
    ailments: str = ""    # free text
    photo: str = ""   # version token of the photo, "" when there is none

    def label(self) -> str:
        years = ""
        if self.birth or self.death:
            years = f" ({self.birth or '?'}-{self.death})" if self.death else f" ({self.birth})"
        return f"{self.name}{years}"


@dataclass
class Marriage:
    a: str
    b: str
    start: str = ""
    end: str = ""


def sniff_image(data: bytes) -> str:
    """Return the MIME type from the file's magic bytes (never trust the client's claim)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise FamilyError("Photo must be a JPEG, PNG or WebP image")


def fit_photo(data: bytes, max_bytes: int, side: int = 400) -> bytes:
    """Return image bytes that fit in max_bytes: unchanged if already small enough,
    otherwise a centre-cropped square JPEG (needs Pillow: pip install pillow)."""
    sniff_image(data)
    if len(data) <= max_bytes:
        return data
    try:
        import io
        from PIL import Image, ImageOps
    except ImportError as e:
        raise FamilyError(f"Photo is {len(data) // 1024} KB (limit {max_bytes // 1024} KB). "
                          "Install Pillow (pip install pillow) to shrink photos automatically.") from e
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        img = Image.alpha_composite(Image.new("RGBA", img.size, "white"), img.convert("RGBA")).convert("RGB")
    except Exception as e:
        raise FamilyError("Could not read that image") from e
    w, h = img.size
    m = min(w, h)
    img = img.crop(((w - m) // 2, (h - m) // 2, (w - m) // 2 + m, (h - m) // 2 + m))
    s = min(side, m)
    while s >= 80:
        small = img.resize((s, s), Image.LANCZOS)
        for q in (85, 75, 65, 55, 45):
            buf = io.BytesIO()
            small.save(buf, "JPEG", quality=q, optimize=True)
            if buf.tell() <= max_bytes:
                return buf.getvalue()
        s = int(s * 0.8)
    raise FamilyError("Could not shrink that photo enough")


class FamilyStore:
    def __init__(self, source):
        """source: a file path (text backend) or a backend object."""
        self.backend = source if hasattr(source, "read") else TextBackend(source)
        self._lock = RLock()
        self.people: dict[str, Person] = {}
        self.marriages: list[Marriage] = []
        self.parents: dict[str, list[str]] = {}   # child -> [parents]
        self.children: dict[str, list[str]] = {}  # parent -> [children]
        self.photos: dict[str, tuple[str, str]] = {}  # person id -> (mime, version)
        self._load(force=True)

    def describe(self) -> str:
        return self.backend.describe()

    # ------------------------------------------------------------------ load
    def _load(self, force: bool = False) -> None:
        with self._lock:
            if not force and not self.backend.stale():
                return
            people: dict[str, Person] = {}
            marriages: list[Marriage] = []
            pairs: list[tuple[str, str]] = []
            photos: dict[str, tuple[str, str]] = {}
            links: list[tuple[str, str, str]] = []
            for kind, f, where in self.backend.read():
                if kind == "PERSON":
                    if len(f) < 2 or not f[0] or not f[1]:
                        raise FamilyError(f"{where}: PERSON needs id and name")
                    if f[0] in people:
                        raise FamilyError(f"{where}: duplicate person id {f[0]}")
                    f = (f + [""] * 8)[:8]          # older 5-column rows still load
                    people[f[0]] = Person(f[0], f[1], f[2].upper(), f[3], f[4],
                                          nakshatra=f[5], gotra=f[6], ailments=f[7])
                elif kind == "MARRIED":
                    if len(f) < 2:
                        raise FamilyError(f"{where}: MARRIED needs two ids")
                    f = (f + [""] * 4)[:4]
                    marriages.append(Marriage(f[0], f[1], f[2], f[3]))
                    links.append((f[0], f[1], where))
                elif kind == "PARENT":
                    if len(f) < 2:
                        raise FamilyError(f"{where}: PARENT needs parent and child ids")
                    pairs.append((f[0], f[1]))
                    links.append((f[0], f[1], where))
                elif kind == "PHOTO":
                    if f and f[0]:
                        f = (f + ["", "", ""])[:3]
                        photos[f[0]] = (f[1], f[2] or "1")
                else:
                    raise FamilyError(f"{where}: unknown record type {kind!r}")
            for pid, (_, ver) in photos.items():
                if pid in people:          # photos of removed people are ignored
                    people[pid].photo = ver
            for a, b, where in links:
                for pid in (a, b):
                    if pid not in people:
                        raise FamilyError(f"{where}: unknown person id {pid!r}")
            parents: dict[str, list[str]] = {}
            children: dict[str, list[str]] = {}
            for p, c in pairs:
                parents.setdefault(c, []).append(p)
                children.setdefault(p, []).append(c)
            self.people, self.marriages = people, marriages
            self.photos = {k: v for k, v in photos.items() if k in people}
            self.parents, self.children = parents, children

    # ---------------------------------------------------------------- writes
    MAX_TEXT = 80   # longest allowed name / nakshatra / gotra
    MAX_LONG = 1000  # longest allowed ailments text

    @classmethod
    def _clean_fields(cls, name: str, gender: str, birth: str, death: str,
                      nakshatra: str = "", gotra: str = "", ailments: str = "") -> tuple[str, str, str, str, str, str, str]:
        name, gender, birth, death = name.strip(), gender.strip().upper(), birth.strip(), death.strip()
        nakshatra, gotra = " ".join(nakshatra.split()), " ".join(gotra.split())   # tidy spacing
        ailments = " ".join(ailments.split())
        if not name:
            raise FamilyError("name is required")
        for v in (name, gender, birth, death, nakshatra, gotra, ailments):
            if "|" in v or "\n" in v or "\r" in v:
                raise FamilyError("'|' and newlines are not allowed in values")
        if gender not in ("", "M", "F", "O"):
            raise FamilyError("gender must be M, F, O or blank")
        for label, v in (("Name", name), ("Nakshatra", nakshatra), ("Gotra", gotra)):
            if len(v) > cls.MAX_TEXT:
                raise FamilyError(f"{label} must be {cls.MAX_TEXT} characters or fewer")
        if len(ailments) > cls.MAX_LONG:
            raise FamilyError(f"Ailments must be {cls.MAX_LONG} characters or fewer")
        return name, gender, birth, death, nakshatra, gotra, ailments

    def add_person(self, name: str, gender: str = "", birth: str = "",
                   death: str = "", person_id: str = "", nakshatra: str = "", gotra: str = "",
                   ailments: str = "") -> Person:
        with self._lock:
            self._load()
            name, gender, birth, death, nakshatra, gotra, ailments = self._clean_fields(
                name, gender, birth, death, nakshatra, gotra, ailments)
            if "|" in person_id or "\n" in person_id:
                raise FamilyError("'|' and newlines are not allowed in values")
            pid = person_id.strip() or self._next_id()
            if pid in self.people:
                raise FamilyError(f"person id {pid} already exists")
            self.backend.add_person([pid, name, gender, birth, death, nakshatra, gotra, ailments])
            self._load(force=True)
            return self.people[pid]

    def update_person(self, person_id: str, name: str | None = None, gender: str | None = None,
                      birth: str | None = None, death: str | None = None,
                      nakshatra: str | None = None, gotra: str | None = None,
                      ailments: str | None = None) -> Person:
        """Change a person's details. None = leave unchanged; "" = clear (not for name)."""
        with self._lock:
            self._load()
            cur = self._need(person_id)
            name, gender, birth, death, nakshatra, gotra, ailments = self._clean_fields(
                cur.name if name is None else name,
                cur.gender if gender is None else gender,
                cur.birth if birth is None else birth,
                cur.death if death is None else death,
                cur.nakshatra if nakshatra is None else nakshatra,
                cur.gotra if gotra is None else gotra,
                cur.ailments if ailments is None else ailments)
            self.backend.update_person([person_id, name, gender, birth, death, nakshatra, gotra, ailments])
            self._load(force=True)
            return self.people[person_id]

    def delete_person(self, person_id: str) -> dict:
        """Remove a person and every marriage, parent/child link and photo that mentions them.
        Other people are untouched (a child simply loses that parent). Cannot be undone."""
        with self._lock:
            self._load()
            p = self._need(person_id)
            summary = {
                "person": asdict(p),
                "marriages": sum(1 for m in self.marriages if person_id in (m.a, m.b)),
                "parent_links": len(self.parents.get(person_id, [])) + len(self.children.get(person_id, [])),
                "photo": person_id in self.photos,
            }
            self.backend.delete_person(person_id)
            self._load(force=True)
            return summary

    # ---------------------------------------------------------------- photos
    @property
    def max_photo_bytes(self) -> int:
        return getattr(self.backend, "max_photo_bytes", 0)

    def set_photo(self, person_id: str, data: bytes) -> Person:
        """Attach/replace a person's photo (JPEG, PNG or WebP, within the backend's size limit)."""
        with self._lock:
            self._load()
            self._need(person_id)
            mime = sniff_image(data)
            if len(data) > self.max_photo_bytes:
                raise FamilyError(f"Photo is too large ({len(data) // 1024} KB); "
                                  f"the limit is {self.max_photo_bytes // 1024} KB")
            self.backend.set_photo(person_id, mime, data)
            self._load(force=True)
            return self.people[person_id]

    def get_photo(self, person_id: str) -> tuple[str, bytes] | None:
        with self._lock:
            self._load()
            self._need(person_id)
            return self.backend.get_photo(person_id) if person_id in self.photos else None

    def delete_photo(self, person_id: str) -> Person:
        with self._lock:
            self._load()
            self._need(person_id)
            self.backend.delete_photo(person_id)
            self._load(force=True)
            return self.people[person_id]

    def add_marriage(self, a: str, b: str, start: str = "", end: str = "") -> Marriage:
        with self._lock:
            self._load()
            self._need(a), self._need(b)
            if a == b:
                raise FamilyError("a person cannot marry themselves")
            if b in self.ancestors(a) or a in self.ancestors(b):
                raise FamilyError("these people are directly related as ancestor/descendant")
            if any({m.a, m.b} == {a, b} and m.start == start for m in self.marriages):
                raise FamilyError("this marriage already exists")
            self.backend.add_marriage([a, b, start, end])
            self._load(force=True)
            return self.marriages[-1]

    def add_parent_child(self, parent: str, child: str) -> None:
        with self._lock:
            self._load()
            self._need(parent), self._need(child)
            if parent == child:
                raise FamilyError("a person cannot be their own parent")
            if child in self.children.get(parent, []):
                raise FamilyError("this parent-child link already exists")
            if len(self.parents.get(child, [])) >= 2:
                raise FamilyError(f"{child} already has two parents")
            if parent in self.descendants(child):
                raise FamilyError("this would create a cycle (parent is a descendant of child)")
            self.backend.add_parent([parent, child])
            self._load(force=True)

    # --------------------------------------------------------------- queries
    def _need(self, pid: str) -> Person:
        self._load()
        if pid not in self.people:
            raise FamilyError(f"unknown person id {pid!r}")
        return self.people[pid]

    def _next_id(self) -> str:
        nums = [int(m.group(1)) for p in self.people if (m := re.fullmatch(r"P(\d+)", p))]
        return f"P{max(nums, default=0) + 1}"

    def get(self, pid: str) -> Person:
        return self._need(pid)

    def search(self, text: str) -> list[Person]:
        self._load()
        t = text.lower().strip()
        return [p for p in self.people.values() if t in p.name.lower() or t == p.id.lower()]

    def get_parents(self, pid: str) -> list[Person]:
        self._need(pid)
        return [self.people[x] for x in self.parents.get(pid, [])]

    def get_children(self, pid: str) -> list[Person]:
        self._need(pid)
        return [self.people[x] for x in self.children.get(pid, [])]

    def get_spouses(self, pid: str) -> list[dict]:
        self._need(pid)
        out = []
        for m in self.marriages:
            if pid in (m.a, m.b):
                other = m.b if m.a == pid else m.a
                out.append({"person": self.people[other], "start": m.start, "end": m.end,
                            "current": not m.end})
        return out

    def get_siblings(self, pid: str) -> list[dict]:
        self._need(pid)
        mine = set(self.parents.get(pid, []))
        seen: dict[str, str] = {}
        for p in mine:
            for c in self.children.get(p, []):
                if c != pid:
                    shared = mine & set(self.parents.get(c, []))
                    kind = "full" if shared == mine == set(self.parents.get(c, [])) else "half"
                    seen[c] = kind
        return [{"person": self.people[c], "type": k} for c, k in seen.items()]

    def ancestors(self, pid: str) -> dict[str, int]:
        """id -> generations up (1 = parent)."""
        self._need(pid)
        out: dict[str, int] = {}
        frontier, gen = [pid], 0
        while frontier:
            gen += 1
            nxt = []
            for x in frontier:
                for p in self.parents.get(x, []):
                    if p not in out:
                        out[p] = gen
                        nxt.append(p)
            frontier = nxt
        return out

    def descendants(self, pid: str) -> dict[str, int]:
        self._need(pid)
        out: dict[str, int] = {}
        frontier, gen = [pid], 0
        while frontier:
            gen += 1
            nxt = []
            for x in frontier:
                for c in self.children.get(x, []):
                    if c not in out:
                        out[c] = gen
                        nxt.append(c)
            frontier = nxt
        return out

    def relationship(self, a: str, b: str) -> str:
        """Describe what b is to a (e.g. 'b is a's grandmother')."""
        pa, pb = self._need(a), self._need(b)
        if a == b:
            return f"{pb.name} is the same person as {pa.name}"
        if any({m.a, m.b} == {a, b} for m in self.marriages):
            return f"{pb.name} is {pa.name}'s {self._g(pb, 'husband', 'wife', 'spouse')}"
        up_a = {a: 0, **self.ancestors(a)}
        up_b = {b: 0, **self.ancestors(b)}
        common = set(up_a) & set(up_b)
        if not common:
            # in-law check: spouse of a blood relative
            for m in self.marriages:
                if b in (m.a, m.b):
                    other = m.b if m.a == b else m.a
                    if other != a and ({other} & (set(up_a) | set(self.descendants(a)))
                                       or self._blood(a, other)):
                        return f"{pb.name} is {pa.name}'s in-law (spouse of {self.people[other].name})"
            return f"No blood relationship found between {pa.name} and {pb.name}"
        best = min(common, key=lambda x: up_a[x] + up_b[x])
        da, db = up_a[best], up_b[best]
        return f"{pb.name} is {pa.name}'s {self._label(pb, da, db, a, b)}"

    def _blood(self, a: str, b: str) -> bool:
        return bool((set(self.ancestors(a)) | {a}) & (set(self.ancestors(b)) | {b}))

    @staticmethod
    def _g(p: Person, m: str, f: str, n: str) -> str:
        return m if p.gender == "M" else f if p.gender == "F" else n

    def _label(self, pb: Person, da: int, db: int, a: str, b: str) -> str:
        g = self._g
        great = lambda n: ("great-" * (n - 2)) + "grand" if n >= 2 else ""
        if db == 0:  # b is an ancestor of a
            base = g(pb, "father", "mother", "parent")
            return (great(da) + base) if da >= 2 else base
        if da == 0:  # b is a descendant of a
            base = g(pb, "son", "daughter", "child")
            return (great(db) + base) if db >= 2 else base
        if da == 1 and db == 1:
            kind = next((s["type"] for s in self.get_siblings(a) if s["person"].id == b), "full")
            word = g(pb, "brother", "sister", "sibling")
            return f"half-{word}" if kind == "half" else word
        if da == 1:  # b is niece/nephew line
            base = g(pb, "nephew", "niece", "nibling")
            return (great(db - 1) + base) if db >= 3 else base
        if db == 1:  # b is aunt/uncle line
            base = g(pb, "uncle", "aunt", "parent's sibling")
            return (great(da - 1) + base) if da >= 3 else base
        k, r = min(da, db) - 1, abs(da - db)
        ordn = {1: "1st", 2: "2nd", 3: "3rd"}.get(k, f"{k}th")
        return f"{ordn} cousin" + (f" {r}x removed" if r else "")

    # ------------------------------------------------------------- rendering
    def render_descendants(self, pid: str, max_depth: int = 10) -> str:
        root = self._need(pid)
        lines: list[str] = []

        def line(p: Person) -> str:
            sp = [f"{s['person'].label()}" + ("" if s["current"] else " [former]")
                  for s in self.get_spouses(p.id)]
            return p.label() + (" + " + ", ".join(sp) if sp else "")

        def walk(p: Person, prefix: str, depth: int, seen: set[str]) -> None:
            kids = self.get_children(p.id)
            if depth >= max_depth:
                if kids:
                    lines.append(prefix + "└── ...")
                return
            for i, c in enumerate(kids):
                last = i == len(kids) - 1
                lines.append(f"{prefix}{'└── ' if last else '├── '}{line(c)}")
                walk(c, prefix + ("    " if last else "│   "), depth + 1, seen)

        lines.append(line(root))
        walk(root, "", 0, set())
        return "\n".join(lines)

    def render_ancestors(self, pid: str, max_depth: int = 10) -> str:
        root = self._need(pid)
        lines = [root.label()]

        def walk(p: str, prefix: str, depth: int) -> None:
            ps = self.parents.get(p, [])
            if depth >= max_depth:
                return
            for i, x in enumerate(ps):
                last = i == len(ps) - 1
                lines.append(f"{prefix}{'└── ' if last else '├── '}{self.people[x].label()}")
                walk(x, prefix + ("    " if last else "│   "), depth + 1)

        walk(pid, "", 0)
        return "\n".join(lines)


def to_dict(p: Person) -> dict:
    return asdict(p)


def open_store(arg: str | None = None) -> FamilyStore:
    """Pick the storage backend.

    arg "gsheet:<id-or-url>"  -> Google Sheet
    arg <path>                -> text file
    no arg: GOOGLE_SHEET_ID env -> Google Sheet, else FAMILY_FILE env, else family.txt
    """
    if arg and arg.startswith("gsheet:"):
        return FamilyStore(SheetBackend.from_env(arg[len("gsheet:"):]))
    if arg:
        return FamilyStore(TextBackend(arg))
    sheet = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    if sheet:
        return FamilyStore(SheetBackend.from_env(sheet))
    return FamilyStore(TextBackend(os.environ.get("FAMILY_FILE", str(DEFAULT_FILE))))
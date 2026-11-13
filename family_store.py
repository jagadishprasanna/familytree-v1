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


class FamilyStore:
    def __init__(self, source):
        """source: a file path (text backend) or a backend object."""
        self.backend = source if hasattr(source, "read") else TextBackend(source)
        self._lock = RLock()
        self.people: dict[str, Person] = {}
        self.marriages: list[Marriage] = []
        self.parents: dict[str, list[str]] = {}   # child -> [parents]
        self.children: dict[str, list[str]] = {}  # parent -> [children]
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
            links: list[tuple[str, str, str]] = []
            for kind, f, where in self.backend.read():
                if kind == "PERSON":
                    if len(f) < 2 or not f[0] or not f[1]:
                        raise FamilyError(f"{where}: PERSON needs id and name")
                    if f[0] in people:
                        raise FamilyError(f"{where}: duplicate person id {f[0]}")
                    f = (f + [""] * 5)[:5]
                    people[f[0]] = Person(f[0], f[1], f[2].upper(), f[3], f[4])
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
                else:
                    raise FamilyError(f"{where}: unknown record type {kind!r}")
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
            self.parents, self.children = parents, children

    # ---------------------------------------------------------------- writes
    @staticmethod
    def _clean_fields(name: str, gender: str, birth: str, death: str) -> tuple[str, str, str, str]:
        name, gender, birth, death = name.strip(), gender.strip().upper(), birth.strip(), death.strip()
        if not name:
            raise FamilyError("name is required")
        for v in (name, gender, birth, death):
            if "|" in v or "\n" in v or "\r" in v:
                raise FamilyError("'|' and newlines are not allowed in values")
        if gender not in ("", "M", "F", "O"):
            raise FamilyError("gender must be M, F, O or blank")
        return name, gender, birth, death

    def add_person(self, name: str, gender: str = "", birth: str = "",
                   death: str = "", person_id: str = "") -> Person:
        with self._lock:
            self._load()
            name, gender, birth, death = self._clean_fields(name, gender, birth, death)
            if "|" in person_id or "\n" in person_id:
                raise FamilyError("'|' and newlines are not allowed in values")
            pid = person_id.strip() or self._next_id()
            if pid in self.people:
                raise FamilyError(f"person id {pid} already exists")
            self.backend.add_person([pid, name, gender, birth, death])
            self._load(force=True)
            return self.people[pid]

    def update_person(self, person_id: str, name: str | None = None, gender: str | None = None,
                      birth: str | None = None, death: str | None = None) -> Person:
        """Change a person's details. None = leave unchanged; "" = clear (not for name)."""
        with self._lock:
            self._load()
            cur = self._need(person_id)
            name, gender, birth, death = self._clean_fields(
                cur.name if name is None else name,
                cur.gender if gender is None else gender,
                cur.birth if birth is None else birth,
                cur.death if death is None else death)
            self.backend.update_person([person_id, name, gender, birth, death])
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
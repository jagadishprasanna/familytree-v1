"""Import a Geni export (GEDCOM .ged) into the family-tree text file.

Geni exports trees as GEDCOM. This module reads that file and produces the
records used by family.txt:

    PERSON  | id | name | gender (M/F/O/blank) | birth | death
    MARRIED | id | id | start | end
    PARENT  | parent_id | child_id

Library use (e.g. from a web upload handler or an MCP tool):

    from geni_import import import_gedcom
    result = import_gedcom(uploaded_bytes)        # bytes or str
    result.people, result.marriages, result.parent_links, result.warnings
    result.to_lines()                             # family.txt records

Command line:

    python geni_import.py export.ged -o family.txt            # new file
    python geni_import.py export.ged -o family.txt --merge    # add to existing
    python geni_import.py export.ged --dry-run                # just report

Re-importing the same file with --merge is safe: ids that already exist are
skipped, so nothing is duplicated.

The web "Import" page uses plan_import() / apply_plan() at the bottom of this file, which work against
whichever storage the site uses (text file or Google Sheet), match people who are already in the tree
(same name and birth date) instead of duplicating them, and attach new relatives to them.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

MONTHS = {m: i for i, m in enumerate(
    "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}
NON_BIRTH = {"adopted", "adoption", "foster", "sealing", "step"}


# --------------------------------------------------------------------------
# GEDCOM parsing
# --------------------------------------------------------------------------
@dataclass
class Node:
    level: int
    tag: str
    value: str = ""
    xref: str = ""
    children: list["Node"] = field(default_factory=list)

    def first(self, tag: str) -> "Node | None":
        return next((c for c in self.children if c.tag == tag), None)

    def all(self, tag: str) -> list["Node"]:
        return [c for c in self.children if c.tag == tag]

    def text(self, tag: str) -> str:
        n = self.first(tag)
        return n.value.strip() if n else ""


def decode_gedcom(data: bytes) -> str:
    """Decode a .ged file: UTF-8 (with/without BOM), UTF-16, else cp1252."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if len(data) > 1 and data[1:2] == b"\x00":
        return data.decode("utf-16-le", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


_LINE = re.compile(r"^\s*(\d+)\s+(?:(@[^@]+@)\s+)?(\S+)(?:\s(.*))?$")


def parse_gedcom(text: str) -> list[Node]:
    """Parse GEDCOM text into a list of level-0 records."""
    roots: list[Node] = []
    stack: list[Node] = []
    for raw in text.lstrip("﻿").splitlines():
        if not raw.strip():
            continue
        m = _LINE.match(raw)
        if not m:
            continue
        level, xref, tag, value = int(m[1]), m[2] or "", m[3].upper(), m[4] or ""
        value = value.replace("@@", "@")
        while stack and stack[-1].level >= level:
            stack.pop()
        if tag in ("CONC", "CONT") and stack:
            stack[-1].value += ("\n" if tag == "CONT" else "") + value
            continue
        node = Node(level, tag, value, xref)
        (stack[-1].children if stack else roots).append(node)
        stack.append(node)
    return roots


# --------------------------------------------------------------------------
# Field cleaning
# --------------------------------------------------------------------------
def _clean(s: str) -> str:
    """Make a value safe for the pipe-delimited file."""
    return re.sub(r"\s+", " ", s.replace("|", "/").replace("#", "")).strip()


def parse_name(indi: Node) -> str:
    n = indi.first("NAME")
    if not n:
        return "Unknown"
    given = sur = suffix = ""
    m = re.match(r"^(.*?)\s*/(.*?)/\s*(.*)$", n.value.strip())
    if m:
        given, sur, suffix = m[1], m[2], m[3]
    else:
        given = n.value
    given = n.text("GIVN") or given
    sur = n.text("SURN") or sur
    suffix = n.text("NSFX") or suffix
    name = _clean(" ".join(p for p in (given, sur, suffix) if p and p.strip()))
    return name or "Unknown"


def parse_gender(indi: Node) -> str:
    s = indi.text("SEX").upper()[:1]
    if s in ("M", "F"):
        return s
    return "O" if s == "X" else ""


def norm_date(raw: str, keep_qualifiers: bool = False) -> str:
    """'12 MAR 1925' -> '1925-03-12', 'MAR 1925' -> '1925-03', 'ABT 1925' -> '1925'.

    Qualifiers (ABT/BEF/AFT/BET..AND) are dropped by default so the value
    stays a plain date/year; keep_qualifiers=True gives 'c.1925', '<1925',
    '>1925', '1920/1925'.
    """
    s = re.sub(r"\(.*?\)", "", raw).strip().upper()
    if not s:
        return ""
    prefix = ""
    second = ""
    rng = re.match(r"^(?:BET|FROM)\s+(.*?)\s+(?:AND|TO)\s+(.*)$", s)
    if rng:
        s, second = rng[1], rng[2]
    else:
        q = re.match(r"^(ABT|ABOUT|CAL|EST|BEF|AFT|TO|FROM)\.?\s+(.*)$", s)
        if q:
            prefix = {"BEF": "<", "AFT": ">"}.get(q[1], "c.")
            s = q[2]

    def one(part: str) -> str:
        m = re.match(r"^(?:(\d{1,2})\s+)?(?:([A-Z]{3})[A-Z]*\.?\s+)?(\d{3,4})(?:/\d{2})?$",
                     part.strip())
        if not m:
            return _clean(part)
        day, mon, year = m[1], m[2], int(m[3])
        out = f"{year:04d}"
        if mon in MONTHS:
            out += f"-{MONTHS[mon]:02d}"
            if day:
                out += f"-{int(day):02d}"
        return out

    result = one(s)
    if keep_qualifiers:
        if second:
            return f"{result}/{one(second)}"
        return prefix + result
    return result


def event_date(node: Node, tag: str, keep_q: bool) -> tuple[bool, str]:
    """(event_present, normalized_date)"""
    ev = node.first(tag)
    if not ev:
        return False, ""
    return True, norm_date(ev.text("DATE"), keep_q)


# --------------------------------------------------------------------------
# Result container
# --------------------------------------------------------------------------
@dataclass
class ImportedPerson:
    id: str
    name: str
    gender: str = ""
    birth: str = ""
    death: str = ""


@dataclass
class ImportResult:
    people: list[ImportedPerson] = field(default_factory=list)
    marriages: list[tuple[str, str, str, str]] = field(default_factory=list)  # a, b, start, end
    parent_links: list[tuple[str, str]] = field(default_factory=list)         # parent, child
    warnings: list[str] = field(default_factory=list)

    def to_lines(self, source: str = "") -> list[str]:
        head = (f"# Imported from Geni GEDCOM{': ' + source if source else ''} "
                f"({len(self.people)} people, {len(self.marriages)} marriages, "
                f"{len(self.parent_links)} parent links)")
        lines = [head]
        for p in self.people:
            lines.append(f"PERSON  | {p.id} | {p.name} | {p.gender} | {p.birth} | {p.death}".rstrip())
        for a, b, start, end in self.marriages:
            lines.append(f"MARRIED | {a} | {b} | {start} | {end}".rstrip())
        for par, ch in self.parent_links:
            lines.append(f"PARENT  | {par} | {ch}")
        return lines

    def summary(self) -> str:
        return (f"{len(self.people)} people, {len(self.marriages)} marriages, "
                f"{len(self.parent_links)} parent-child links, "
                f"{len(self.warnings)} warnings")


# --------------------------------------------------------------------------
# Conversion
# --------------------------------------------------------------------------
def import_gedcom(data: "bytes | str", prefix: str = "G", birth_only: bool = False,
                  keep_qualifiers: bool = False) -> ImportResult:
    """Convert GEDCOM (bytes or text) to family-tree records.

    birth_only: ignore adopted/foster/step parent links.
    """
    text = decode_gedcom(data) if isinstance(data, (bytes, bytearray)) else data
    records = parse_gedcom(text)
    res = ImportResult()

    # ---- people ----------------------------------------------------------
    ids: dict[str, str] = {}   # gedcom xref -> our id
    used: set[str] = set()
    for rec in records:
        if rec.tag != "INDI" or not rec.xref:
            continue
        base = re.sub(r"[^A-Za-z0-9_-]", "", rec.xref.strip("@"))
        base = re.sub(r"^[IiFf](?=\d)", "", base) or "X"
        pid, n = prefix + base, 2
        while pid in used:
            pid, n = f"{prefix}{base}_{n}", n + 1
        used.add(pid)
        ids[rec.xref] = pid
        _, birth = event_date(rec, "BIRT", keep_qualifiers)
        has_death, death = event_date(rec, "DEAT", keep_qualifiers)
        if has_death and not death:
            death = "?"
        res.people.append(ImportedPerson(pid, parse_name(rec), parse_gender(rec), birth, death))

    indi_by_xref = {r.xref: r for r in records if r.tag == "INDI"}

    # ---- parent links ----------------------------------------------------
    # per child: list of (parent_id, non_birth) in file order
    per_child: dict[str, list[tuple[str, bool]]] = {}
    fam_records = [r for r in records if r.tag == "FAM"]
    for fam in fam_records:
        spouses = []
        for tag in ("HUSB", "WIFE"):
            ref = fam.text(tag)
            if not ref:
                continue
            if ref in ids:
                spouses.append((tag, ids[ref]))
            else:
                res.warnings.append(f"{fam.xref}: {tag} {ref} not found; skipped")
        for chil in fam.all("CHIL"):
            cref = chil.value.strip()
            if cref not in ids:
                res.warnings.append(f"{fam.xref}: child {cref} not found; skipped")
                continue
            # pedigree: INDI.FAMC.PEDI for this family, plus CHIL._FREL/_MREL
            ped = ""
            for famc in indi_by_xref[cref].all("FAMC"):
                if famc.value.strip() == fam.xref:
                    ped = famc.text("PEDI").lower()
            for tag, pid in spouses:
                rel = chil.text("_FREL" if tag == "HUSB" else "_MREL").lower()
                non_birth = (ped in NON_BIRTH) or (rel in NON_BIRTH)
                if non_birth and birth_only:
                    continue
                per_child.setdefault(ids[cref], []).append((pid, non_birth))

    children: dict[str, list[str]] = {}

    def reaches(a: str, b: str) -> bool:
        """True if b is a descendant of a."""
        seen, todo = set(), [a]
        while todo:
            cur = todo.pop()
            for c in children.get(cur, []):
                if c == b:
                    return True
                if c not in seen:
                    seen.add(c)
                    todo.append(c)
        return False

    names = {p.id: p.name for p in res.people}
    for child, plist in per_child.items():
        plist = sorted(plist, key=lambda t: t[1])      # birth parents first
        seen: list[str] = []
        for pid, _ in plist:
            if pid not in seen:
                seen.append(pid)
        if len(seen) > 2:
            res.warnings.append(f"{names[child]} ({child}) has {len(seen)} parents; "
                                f"kept the first 2 (birth parents first)")
            seen = seen[:2]
        for pid in seen:
            if pid == child or reaches(child, pid):
                res.warnings.append(f"Parent loop: {names[pid]} ({pid}) -> {names[child]} "
                                    f"({child}); link skipped")
                continue
            children.setdefault(pid, []).append(child)
            res.parent_links.append((pid, child))

    # ---- marriages -------------------------------------------------------
    seen_pairs: set[frozenset] = set()
    for fam in fam_records:
        h, w = fam.text("HUSB"), fam.text("WIFE")
        if h not in ids or w not in ids:
            continue
        a, b = ids[h], ids[w]
        key = frozenset((a, b))
        if a == b or key in seen_pairs:
            continue
        if reaches(a, b) or reaches(b, a):
            res.warnings.append(f"Skipped marriage {names[a]} + {names[b]}: "
                                f"one is a direct ancestor of the other")
            continue
        seen_pairs.add(key)
        _, start = event_date(fam, "MARR", keep_qualifiers)
        has_div, end = event_date(fam, "DIV", keep_qualifiers)
        if has_div and not end:
            end = "divorced"
        res.marriages.append((a, b, start, end))

    return res


# --------------------------------------------------------------------------
# Writing / merging into family.txt
# --------------------------------------------------------------------------
def _existing_records(path: Path):
    ids, marriages, parents, people = set(), set(), set(), set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        f = [x.strip() for x in line.split("|")]
        kind = f[0].upper()
        if kind == "PERSON" and len(f) >= 3:
            ids.add(f[1])
            people.add((f[2].lower(), f[4] if len(f) > 4 else ""))
        elif kind == "MARRIED" and len(f) >= 3:
            marriages.add(frozenset((f[1], f[2])))
        elif kind == "PARENT" and len(f) >= 3:
            parents.add((f[1], f[2]))
    return ids, marriages, parents, people


def write_family_file(res: ImportResult, path: "str | Path", merge: bool = False,
                      source: str = "") -> ImportResult:
    """Write records to path. With merge=True append only what is new.

    Returns the ImportResult that was actually written (after skipping
    anything already in the file).
    """
    path = Path(path)
    if path.exists() and not merge:
        raise FileExistsError(f"{path} exists; use merge=True to add to it")
    if not (path.exists() and merge):
        path.write_text("\n".join(res.to_lines(source)) + "\n", encoding="utf-8")
        return res

    ids, marriages, parents, people = _existing_records(path)
    new = ImportResult(warnings=list(res.warnings))
    for p in res.people:
        if p.id in ids:
            continue
        if (p.name.lower(), p.birth) in people and p.birth:
            new.warnings.append(f"Possible duplicate of an existing person: {p.name} ({p.birth})")
        new.people.append(p)
    new_ids = ids | {p.id for p in new.people}
    for a, b, s, e in res.marriages:
        if frozenset((a, b)) not in marriages and a in new_ids and b in new_ids:
            new.marriages.append((a, b, s, e))
    for par, ch in res.parent_links:
        if (par, ch) not in parents and par in new_ids and ch in new_ids:
            new.parent_links.append((par, ch))
    if new.people or new.marriages or new.parent_links:
        existing = path.read_text(encoding="utf-8")
        sep = "" if existing.endswith("\n") or not existing else "\n"
        path.write_text(existing + sep + "\n".join(new.to_lines(source)) + "\n", encoding="utf-8")
    return new


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Import a Geni GEDCOM export into family.txt")
    ap.add_argument("gedcom", help="the .ged file exported from Geni")
    ap.add_argument("-o", "--out", help="family.txt to write (default: print a report only)")
    ap.add_argument("--merge", action="store_true",
                    help="add to an existing family.txt instead of refusing to overwrite")
    ap.add_argument("--prefix", default="G", help="id prefix for imported people (default G)")
    ap.add_argument("--birth-only", action="store_true",
                    help="skip adopted/foster/step parent links")
    ap.add_argument("--keep-qualifiers", action="store_true",
                    help="keep 'about/before/after' in dates (c.1900, <1900, >1900)")
    ap.add_argument("--dry-run", action="store_true", help="parse and report; write nothing")
    args = ap.parse_args(argv)

    src = Path(args.gedcom)
    res = import_gedcom(src.read_bytes(), prefix=args.prefix,
                        birth_only=args.birth_only, keep_qualifiers=args.keep_qualifiers)
    if not res.people:
        print("No people found. Is this a GEDCOM (.ged) file exported from Geni?", file=sys.stderr)
        return 1
    written = res
    if args.out and not args.dry_run:
        try:
            written = write_family_file(res, args.out, merge=args.merge, source=src.name)
        except FileExistsError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2
        print(f"Wrote {args.out}: {written.summary()}")
    else:
        print(f"Parsed {src.name}: {res.summary()}")
    for w in written.warnings:
        print(f"  warning: {w}")
    return 0


# --------------------------------------------------------------------------
# Web import: plan against the live tree, then apply (works with text file or Google Sheet)
# --------------------------------------------------------------------------
MAX_IMPORT_PEOPLE = 5000
MAX_NAME = 80


@dataclass
class Plan:
    people: list[ImportedPerson] = field(default_factory=list)            # new people, final ids
    marriages: list[tuple[str, str, str, str]] = field(default_factory=list)
    parent_links: list[tuple[str, str]] = field(default_factory=list)
    matched: list[tuple[str, str]] = field(default_factory=list)          # (imported name, existing id)
    skipped_marriages: int = 0                                            # already in the tree
    skipped_parent_links: int = 0
    total: int = 0                                                        # people in the file
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict:
        return {"in_file": self.total, "new_people": len(self.people), "matched": len(self.matched),
                "new_marriages": len(self.marriages), "new_parent_links": len(self.parent_links),
                "already_marriages": self.skipped_marriages, "already_parent_links": self.skipped_parent_links}


def _fit(s: str) -> str:
    return _clean(s)[:MAX_NAME]


def plan_import(res: ImportResult, store) -> Plan:
    """Work out what would change in `store`, without changing it.

    * A person already in the tree with the same id (an earlier import) or the same name and birth date
      is matched, not duplicated, and new relatives are attached to them.
    * A new person whose id is taken gets a free one.
    * Links that would give someone a third parent, a loop, or a marriage between direct ancestors
      are skipped with a warning.
    """
    store._load()
    if len(res.people) > MAX_IMPORT_PEOPLE:
        raise ValueError(f"This file has {len(res.people)} people; the limit per import is {MAX_IMPORT_PEOPLE}.")
    plan = Plan(total=len(res.people), warnings=list(res.warnings))
    existing = store.people
    by_key = {(p.name.lower(), p.birth): pid for pid, p in existing.items() if p.birth}
    taken = set(existing)
    idmap: dict[str, str] = {}                 # imported id -> final id in the tree
    for ip in res.people:
        name = _fit(ip.name) or "Unknown"
        birth = _fit(ip.birth)
        if ip.id in existing and existing[ip.id].name.lower() == name.lower():
            idmap[ip.id] = ip.id
            plan.matched.append((name, ip.id))
            continue
        hit = by_key.get((name.lower(), birth)) if birth else None
        if hit:
            idmap[ip.id] = hit
            plan.matched.append((name, hit))
            continue
        pid, n = ip.id, 2
        while pid in taken:
            pid, n = f"{ip.id}_{n}", n + 1
        taken.add(pid)
        idmap[ip.id] = pid
        plan.people.append(ImportedPerson(pid, name, ip.gender if ip.gender in ("M", "F", "O") else "",
                                          birth, _fit(ip.death)))
    names = {p.id: p.name for p in plan.people} | {i: p.name for i, p in existing.items()}

    parents = {c: list(v) for c, v in store.parents.items()}
    children = {p: list(v) for p, v in store.children.items()}

    def below(a: str, b: str) -> bool:                    # is b a descendant of a?
        seen, todo = set(), [a]
        while todo:
            for c in children.get(todo.pop(), []):
                if c == b:
                    return True
                if c not in seen:
                    seen.add(c)
                    todo.append(c)
        return False

    for par, ch in res.parent_links:
        par, ch = idmap.get(par), idmap.get(ch)
        if not par or not ch or par == ch:
            continue
        if ch in children.get(par, []):
            plan.skipped_parent_links += 1
            continue
        if len(parents.get(ch, [])) >= 2:
            plan.warnings.append(f"{names[ch]} already has two parents; skipped the link from {names[par]}")
            continue
        if below(ch, par):
            plan.warnings.append(f"Skipped {names[par]} as parent of {names[ch]}: it would create a loop")
            continue
        parents.setdefault(ch, []).append(par)
        children.setdefault(par, []).append(ch)
        plan.parent_links.append((par, ch))

    pairs = {frozenset((m.a, m.b)) for m in store.marriages}
    for a, b, start, end in res.marriages:
        a, b = idmap.get(a), idmap.get(b)
        if not a or not b or a == b:
            continue
        key = frozenset((a, b))
        if key in pairs:
            plan.skipped_marriages += 1
            continue
        if below(a, b) or below(b, a):
            plan.warnings.append(f"Skipped marriage {names[a]} + {names[b]}: one is a direct ancestor of the other")
            continue
        pairs.add(key)
        plan.marriages.append((a, b, _fit(start), _fit(end)))
    return plan


def apply_plan(plan: Plan, store) -> dict:
    """Write a plan into the store in one go (one request per tab on Google Sheets)."""
    store.bulk_add([[p.id, p.name, p.gender, p.birth, p.death, "", "", ""] for p in plan.people],
                   [list(m) for m in plan.marriages],
                   [list(l) for l in plan.parent_links])
    return plan.counts()


if __name__ == "__main__":
    raise SystemExit(main())
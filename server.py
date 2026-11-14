"""MCP server exposing a text-file family tree to Claude.

Run (stdio):   python server.py [path/to/family.txt | gsheet:<sheet-id>]
Env:           GOOGLE_SHEET_ID + GOOGLE_SERVICE_ACCOUNT_JSON (or _FILE) -> Google Sheet
               FAMILY_FILE=/path/to/family.txt                          -> text file
"""
from __future__ import annotations

import sys

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

from pathlib import Path

from family_store import FamilyError, fit_photo, open_store, to_dict

# arg: path to family.txt, or "gsheet:<id>"; default: GOOGLE_SHEET_ID env, else FAMILY_FILE/family.txt
store = open_store(sys.argv[1] if len(sys.argv) > 1 else None)
mcp = FastMCP("family-tree")


# ----------------------------------------------------------------- lookups
@mcp.tool()
def list_people() -> list[dict]:
    """List every person in the family tree."""
    store._load()
    return [to_dict(p) for p in store.people.values()]


@mcp.tool()
def find_person(query: str) -> list[dict]:
    """Find people by (partial) name or exact id."""
    return [to_dict(p) for p in store.search(query)]


@mcp.tool()
def get_person(person_id: str) -> dict:
    """Get a person with their parents, spouses, children and siblings."""
    p = store.get(person_id)
    return {
        **to_dict(p),
        "parents": [to_dict(x) for x in store.get_parents(person_id)],
        "spouses": [{**to_dict(s["person"]), "start": s["start"], "end": s["end"],
                     "current": s["current"]} for s in store.get_spouses(person_id)],
        "children": [to_dict(x) for x in store.get_children(person_id)],
        "siblings": [{**to_dict(s["person"]), "type": s["type"]}
                     for s in store.get_siblings(person_id)],
    }


@mcp.tool()
def get_parents(person_id: str) -> list[dict]:
    """Parents of a person."""
    return [to_dict(p) for p in store.get_parents(person_id)]


@mcp.tool()
def get_children(person_id: str) -> list[dict]:
    """Children of a person."""
    return [to_dict(p) for p in store.get_children(person_id)]


@mcp.tool()
def get_spouses(person_id: str) -> list[dict]:
    """Husband(s)/wife(s) of a person, including former marriages."""
    return [{**to_dict(s["person"]), "start": s["start"], "end": s["end"],
             "current": s["current"]} for s in store.get_spouses(person_id)]


@mcp.tool()
def get_siblings(person_id: str) -> list[dict]:
    """Full and half siblings of a person."""
    return [{**to_dict(s["person"]), "type": s["type"]} for s in store.get_siblings(person_id)]


@mcp.tool()
def get_ancestors(person_id: str) -> list[dict]:
    """All ancestors with generations_up (1 = parent, 2 = grandparent...)."""
    return [{**to_dict(store.people[i]), "generations_up": g}
            for i, g in sorted(store.ancestors(person_id).items(), key=lambda kv: kv[1])]


@mcp.tool()
def get_descendants(person_id: str) -> list[dict]:
    """All descendants with generations_down (1 = child, 2 = grandchild...)."""
    return [{**to_dict(store.people[i]), "generations_down": g}
            for i, g in sorted(store.descendants(person_id).items(), key=lambda kv: kv[1])]


@mcp.tool()
def find_relationship(person_id: str, other_id: str) -> str:
    """Explain what `other_id` is to `person_id` (e.g. grandmother, 2nd cousin, wife)."""
    return store.relationship(person_id, other_id)


@mcp.tool()
def show_descendant_tree(person_id: str, max_depth: int = 10) -> str:
    """ASCII tree of a person's descendants, with spouses shown beside each person."""
    return store.render_descendants(person_id, max_depth)


@mcp.tool()
def show_ancestor_tree(person_id: str, max_depth: int = 10) -> str:
    """ASCII tree of a person's ancestors."""
    return store.render_ancestors(person_id, max_depth)


# ------------------------------------------------------------------ writes
@mcp.tool()
def add_person(name: str, gender: str = "", birth: str = "", death: str = "",
               person_id: str = "") -> dict:
    """Add a person. gender: M/F/O. Dates are free text (e.g. 1950 or 1950-03-01).
    person_id is auto-generated (P1, P2, ...) when omitted."""
    return to_dict(store.add_person(name, gender, birth, death, person_id))


@mcp.tool()
def update_person(person_id: str, name: str | None = None, gender: str | None = None,
                  birth: str | None = None, death: str | None = None) -> dict:
    """Edit a person's details. Omit a field to keep it; pass "" to clear gender/birth/death."""
    return to_dict(store.update_person(person_id, name, gender, birth, death))


@mcp.tool()
def set_person_photo(person_id: str, image_path: str) -> dict:
    """Set a person's photo from an image file on this computer (JPEG/PNG/WebP).
    Large photos are shrunk to a small square automatically (needs `pip install pillow`)."""
    p = Path(image_path).expanduser()
    if not p.is_file():
        raise FamilyError(f"No such file: {image_path}")
    data = fit_photo(p.read_bytes(), store.max_photo_bytes)
    return to_dict(store.set_photo(person_id, data))


@mcp.tool()
def remove_person_photo(person_id: str) -> dict:
    """Remove a person's photo."""
    return to_dict(store.delete_photo(person_id))


@mcp.tool()
def delete_person(person_id: str) -> dict:
    """PERMANENTLY remove a person, plus their marriages, parent/child links and photo.
    Other people stay. Cannot be undone, so confirm with the user before calling this."""
    return store.delete_person(person_id)


@mcp.tool()
def add_marriage(person_id: str, spouse_id: str, start: str = "", end: str = "") -> dict:
    """Record a husband/wife relationship. Set `end` for divorce or widowhood."""
    m = store.add_marriage(person_id, spouse_id, start, end)
    return {"a": m.a, "b": m.b, "start": m.start, "end": m.end}


@mcp.tool()
def add_parent_child(parent_id: str, child_id: str) -> str:
    """Record that parent_id is a parent of child_id (max two parents per child)."""
    store.add_parent_child(parent_id, child_id)
    return f"Recorded: {parent_id} is a parent of {child_id}"


if __name__ == "__main__":
    mcp.run()
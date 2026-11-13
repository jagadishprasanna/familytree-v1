# Family Tree MCP Server

An MCP server that lets Claude read and edit a family tree stored in a plain text file.

## Data file (`family.txt`)
Pipe-delimited, one record per line, `#` for comments:

    PERSON  | id | name | gender (M/F/O) | birth | death
    MARRIED | id | id | start | end          # husband/wife (set end for divorce/death)
    PARENT  | parent_id | child_id           # parent -> child (max 2 parents per child)

You can edit the file by hand while the server runs; it reloads on change.
Writes made through MCP are appended, so your comments and ordering are kept.

## Run
    pip install -r requirements.txt
    python server.py family.txt        # or set FAMILY_FILE=/path/to/family.txt
    python test_family.py              # unit tests

## Claude Desktop config
    {
      "mcpServers": {
        "family-tree": {
          "command": "python",
          "args": ["/abs/path/server.py", "/abs/path/family.txt"]
        }
      }
    }

## Tools
Read: list_people, find_person, get_person, get_parents, get_children, get_spouses,
get_siblings, get_ancestors, get_descendants, find_relationship,
show_descendant_tree, show_ancestor_tree
Write: add_person, add_marriage, add_parent_child

Validation: unknown ids, duplicate ids/links, self-parenting, parent/child cycles,
more than two parents, and marrying a direct ancestor/descendant are rejected.
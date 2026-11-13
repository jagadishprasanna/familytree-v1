# Family Tree: MCP server + web UI, backed by a Google Sheet (or a text file)

Files: `server.py` (MCP server for Claude), `web.py` + `web/index.html` (browser UI),
`family_store.py` (tree logic), `backends.py` (Google Sheet / text file storage),
`migrate_to_sheet.py` (import family.txt into the sheet), `test_family.py`.

## 1. Google Sheet setup (one time, ~5 minutes)
1. Go to https://console.cloud.google.com, create or pick a project, and enable the **Google Sheets API**
   (APIs & Services -> Library).
2. IAM & Admin -> **Service Accounts** -> Create. Open it -> **Keys** -> Add key -> JSON. A key file downloads.
3. Create a blank Google Sheet. Click **Share** and add the service account's email
   (the `client_email` value in the key file) as **Editor**.
4. Copy the sheet id from its URL: `https://docs.google.com/spreadsheets/d/<THIS PART>/edit`.

The app creates three tabs on first run (you can also type in them directly):

| Tab | Columns |
|---|---|
| People | id, name, gender (M/F/O), birth, death |
| Marriages | person_a, person_b, start, end  (fill `end` for divorce/death) |
| Parents | parent_id, child_id  (max two parents per child) |

## 2. Configure
    export GOOGLE_SHEET_ID="<sheet id or full URL>"
    export GOOGLE_SERVICE_ACCOUNT_FILE="/path/to/key.json"      # or GOOGLE_SERVICE_ACCOUNT_JSON='{...whole json...}'
    pip install -r requirements.txt

Optional: `SHEET_CACHE_SECONDS` (default 15) - how long reads are cached; edits made through the app show up immediately.

Import the sample/your existing data once:

    python migrate_to_sheet.py family.txt "$GOOGLE_SHEET_ID"      # --force to overwrite a non-empty sheet

## 3. Run
    python web.py                    # http://127.0.0.1:8000   (Google Sheet when GOOGLE_SHEET_ID is set)
    python server.py                 # MCP server (stdio)
    python web.py family.txt         # a file argument forces the old text-file mode
    python test_family.py            # tests (run against both backends, using an in-memory fake Sheets API)

## Render
- Build Command: `pip install -r requirements-web.txt`
- Start Command: `python web.py --host 0.0.0.0 --port $PORT`   (no file argument)
- Environment: `GOOGLE_SHEET_ID`, and `GOOGLE_SERVICE_ACCOUNT_JSON` = the full contents of the key file.
- Never commit the key file to Git. There is no login on the site: anyone with the URL can view and edit.

## Claude Desktop (MCP)
    {
      "mcpServers": {
        "family-tree": {
          "command": "/full/path/to/python",
          "args": ["/full/path/server.py"],
          "env": {
            "GOOGLE_SHEET_ID": "<sheet id>",
            "GOOGLE_SERVICE_ACCOUNT_FILE": "/full/path/key.json"
          }
        }
      }
    }

Claude Code: `claude mcp add family-tree -e GOOGLE_SHEET_ID=<id> -e GOOGLE_SERVICE_ACCOUNT_FILE=/path/key.json -- python /path/server.py`

## MCP tools
Read: list_people, find_person, get_person, get_parents, get_children, get_spouses, get_siblings,
get_ancestors, get_descendants, find_relationship, show_descendant_tree, show_ancestor_tree.
Write: add_person, update_person, add_marriage, add_parent_child.
Validation rejects unknown ids, duplicates, parent/child cycles, more than two parents, and marrying an ancestor/descendant.

## Text-file format (optional local mode)
    PERSON  | id | name | gender | birth | death
    MARRIED | id | id | start | end
    PARENT  | parent_id | child_id
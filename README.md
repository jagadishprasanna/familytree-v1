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

The app creates these tabs on first run (plus a `Photos` tab, see below) (you can also type in them directly):

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

## Deleting people
Click a person, then **Delete person...** at the bottom of the side panel. A confirmation spells out what goes with them
(their marriages, parent/child links and photo); everyone else stays. A child just loses that parent, so deleting a link
in the middle of a family line can leave the branches below it disconnected. Deleting cannot be undone.
In Google Sheet mode the person's rows are removed from all tabs in one request, so it either fully succeeds or changes nothing.
If Google refuses to delete the last rows under a frozen header, use View > Freeze > No rows in the sheet and retry.
Claude can do the same with the `delete_person` tool (it should ask you first).

## Photos
Click a person, then **Add photo** (or click the empty picture circle). Pick any JPG/PNG/WebP: the browser
crops it to a square and shrinks it before upload, so a 5 MB phone picture becomes ~15 KB. Photos appear in the
tree cards and as a larger picture in the side panel; **Remove photo** deletes it.

- **Google Sheet mode:** photos are kept in a fourth tab, `Photos` (person_id, mime, version, data), created
  automatically. Each photo is stored as text inside one cell, which is why they are limited to ~33 KB
  (400x400 pixels is still plenty for a tree). They survive Render restarts because they live in the sheet.
  Don't edit that tab by hand. Deleting a row simply removes that photo.
- **Text-file mode:** photos are files in a folder next to the data file (`family.txt` -> `family_photos/P7.jpg`).
  You can also drop a file named after the person's id into that folder.
- Claude can set a photo from a file on your computer with `set_person_photo` (and `remove_person_photo`);
  it shrinks big images using Pillow.
- Uploading is available to every logged-in user, like other edits.

## Login
The site is protected by a login page when these environment variables are set (otherwise login is OFF - local use only):

| Variable | Meaning |
|---|---|
| `APP_USERNAME`, `APP_PASSWORD` | the login |
| `APP_USERS` | several logins instead: `{"alice":"pw1","bob":"pw2"}` |
| `SESSION_SECRET` | long random string (e.g. `python -c "import secrets;print(secrets.token_urlsafe(48))"`). Without it, everyone is logged out on each restart. Change it to log everyone out. |
| `SESSION_DAYS` | session length, default 7 |

Logged-out visitors are redirected to `/login` and never receive the page or any data (`/api/*` returns 401).
Sessions are signed, HttpOnly cookies; 5 failed sign-ins from one address lock it out for 15 minutes.
Every logged-in user can view and edit. Use a long password, and HTTPS (Render provides it).

## Render
- Build Command: `pip install -r requirements-web.txt`
- Start Command: `python web.py --host 0.0.0.0 --port $PORT`   (no file argument)
- Environment: `GOOGLE_SHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON` = the full contents of the key file, plus `APP_USERNAME`, `APP_PASSWORD`, `SESSION_SECRET`.
- Never commit the key file or passwords to Git.

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
Write: add_person, update_person, delete_person, set_person_photo, remove_person_photo, add_marriage, add_parent_child.
Validation rejects unknown ids, duplicates, parent/child cycles, more than two parents, and marrying an ancestor/descendant.

## Text-file format (optional local mode)
    PERSON  | id | name | gender | birth | death
    MARRIED | id | id | start | end
    PARENT  | parent_id | child_id
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
| People | id, name, gender (M/F/O), birth, death, nakshatra, gotra, ailments |
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

## Nakshatra and Gotra
Every person has two optional fields, **Nakshatra** (birth star) and **Gotra**. Set them in the **Edit** form or when
adding someone; they appear in a small details box in the side panel. Both are free text, so spelling variants are fine;
the boxes also suggest the 27 nakshatras and common gotras (plus anything already used in your tree).
When you add a child, the Gotra box is pre-filled from the father (editable), since gotra passes through the father's line.
There is also a free-text **Ailments** field (up to 1000 characters; line breaks become spaces, and `|` is not allowed).
An existing sheet keeps working: the `nakshatra`, `gotra` and `ailments` column headings are added to the People tab on the next
start (columns F, G and H), and older text-file lines with only five fields still load.

## Phones and tablets
The site adapts to the screen. On a phone the controls (Ancestors / Descendants, generations, Fit) sit in a bar at the
bottom, a person's details open as a sheet that slides up from the bottom (with a Close button), and forms use large
touch-friendly fields. In the tree, drag with one finger to pan and pinch with two fingers to zoom; on a large tree the
view starts at readable size, centred on the person you chose. In the Time Capsule, swipe left or right to change photos.
On a computer nothing changes: mouse drag, scroll wheel zoom, and the side panel on the right.

## Time Capsule (photo carousel from Google Drive)
The **Time Capsule** tab shows a slideshow of photos kept in a Google Drive folder: previous/next buttons, a thumbnail
strip, Play/Pause (5 seconds per photo), full screen, arrow keys, and swipe on phones. Photos are shown oldest first by
the date the photo was taken (falling back to the upload date). The caption is the file's Drive description if it has one,
otherwise the file name (`Beach_trip-1999.jpg` becomes "Beach trip 1999").

Set-up (once):
1. Create a Drive folder and put the photos in it (JPEG, PNG, WebP or GIF; HEIC is skipped because browsers cannot show it).
2. Share the folder with the service account's email (Viewer is enough), the same one the Google Sheet uses.
3. In Google Cloud (the project that owns the service account) enable the **Google Drive API**.
4. Set `DRIVE_FOLDER_ID` on the server to the folder's id or its link. On Render: Environment, add `DRIVE_FOLDER_ID`.

The server downloads and resizes photos itself (needs Pillow, which is in `requirements-web.txt`), so visitors never get
Drive links, and only people who are logged in can see the photos. New photos appear within about a minute
(`CAPSULE_LIST_SECONDS`). Other settings: `CAPSULE_CACHE_MB` (default 48), `CAPSULE_MAX_PHOTOS` (default 500).

### Adding and deleting photos (Time Capsule)
Signed-in users see **+ Add photos**, **Edit caption / date** and **Delete** above the carousel.
- **Add photos:** pick one or many pictures (JPEG, PNG, WebP, GIF). Originals up to 10 MB are uploaded untouched, so the date
  the photo was taken is kept; bigger ones are shrunk first. iPhone HEIC photos only work if the browser can read them
  (Safari can; otherwise share them as JPEG).
- **Edit caption / date:** the caption is the Drive file's description; the date (used for the order and shown under the
  caption) is stored on the file. Clear the date to go back to the date the photo was taken. Useful for scanned old photos.
- **Delete:** asks first, then moves the photo to the **Google Drive trash**, where it stays 30 days (restore it from Drive).
  Only photos inside the Time Capsule folder can be changed.

**One-time set-up for uploads.** A Google service account has no storage of its own, so it can *read* your folder but cannot
create files in an ordinary Drive folder (Google answers "storage quota exceeded"). Uploads, edits and deletes therefore
use your own Google account:
1. In Google Cloud Console (same project as the service account): **APIs & Services > OAuth consent screen**. User type
   **External**, fill in an app name and your email, add yourself as a test user, then press **Publish app** so the status is
   **In production** (in "Testing" Google expires the sign-in after 7 days; a personal unverified app is fine).
2. **Credentials > Create credentials > OAuth client ID > Desktop app**. Download the JSON.
3. On your own computer run `pip install google-auth-oauthlib`, then `python drive_authorize.py client_secret_XXXX.json`.
   Sign in with the Google account that owns the Time Capsule folder (click Advanced > Go to the app if Google warns).
4. The script prints three lines. Add them in Render under **Environment**: `DRIVE_OAUTH_CLIENT_ID`,
   `DRIVE_OAUTH_CLIENT_SECRET`, `DRIVE_OAUTH_REFRESH_TOKEN`. Keep them private and never commit them to GitHub. Redeploy.
The Render log then shows `Time Capsule editing: ON`. Everyone who can sign in to the site can then add, edit and delete
photos. To switch editing off, set `CAPSULE_READONLY=1` (the buttons disappear). If the folder is inside a Google Workspace
**Shared Drive**, you can skip the sign-in: add the service account to that Shared Drive as Content manager.

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
    PERSON  | id | name | gender | birth | death | nakshatra | gotra | ailments
    MARRIED | id | id | start | end
    PARENT  | parent_id | child_id

## Ask tab (chat assistant)

The **Ask** tab answers plain-language questions about your family: *"How are Emily and Rachel related?"*,
*"Who has the Rohini nakshatra?"*, *"Who was born between 1950 and 1960?"*, *"Which photos are from 1999?"*.
Answers show clickable name chips that open the person in the tree. It is **read-only**: it cannot add, change or delete anything.

How it works: Claude is given a short roster (id, name, years) and *lookup tools* over your tree (search, a person with
parents/spouses/children/siblings, relationship, ancestors, descendants, Time Capsule captions). It looks facts up
instead of guessing, and relationships come from the same code the tree uses.

Turn it on by setting this on Render and redeploying:

| Variable | Meaning |
|---|---|
| `ANTHROPIC_API_KEY` | required; create one at console.anthropic.com |
| `CHAT_MODEL` | optional, default `claude-sonnet-5-5` |
| `CHAT_SHARE_HEALTH` | `1` (default) lets the assistant read the Ailments field; `0` hides it |
| `CHAT_PER_HOUR` | questions per signed-in user per hour, default 30 |

Without the key the tab simply says it is not switched on.

**Privacy and cost:** each question sends the roster and the records needed to answer it to the Anthropic API, and each
question is billed to your API account. Keep login on (`FAMILY_USERS`) so only your family can use it, and set
`CHAT_SHARE_HEALTH=0` if you do not want health notes leaving your server.

Run the tests (no API key needed, they use a fake model): `python test_chat.py`

## Photo descriptions and people (Time Capsule)

Click **Description & people** on any photo to add a short description and tag who is in it (pick names from your tree).
The description and the tagged people show over the photo, and each name opens that person in the tree. The Ask
assistant can then answer things like "Which photos is Emily in?" or "Is there a picnic photo?".

These are stored next to your family data, not in Drive, so they work even with `CAPSULE_READONLY=1` and no Google
sign-in: in a **PhotoNotes** tab of your Google Sheet (created automatically; the service account already has edit
access), or in `<family file>.photonotes.json` when you use the text file. Deleting a photo through the app removes
its note; if you delete a person from the tree they simply stop appearing as a tag.

Tests: `python test_photo_notes.py`
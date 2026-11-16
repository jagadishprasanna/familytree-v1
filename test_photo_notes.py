import http.client
import json
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import web
from backends import SheetBackend, TextBackend, records_to_rows
from chat_agent import ChatAgent
from drive_photos import DrivePhotos
from fake_drive import FakeDriveSession
from fake_sheets import FakeSpreadsheet
from family_store import FamilyStore
from photo_notes import JsonNotes, SheetNotes, open_notes
from test_capsule import files
from test_chat import FakeClient, text, use

SAMPLE = Path(__file__).with_name("family.txt")


class NotesStoreTests(unittest.TestCase):
    def check(self, n):
        self.assertEqual(n.all(), {})
        n.set("a1", "Beach day", ["P1", "P2"])
        n.set("b2", "Wedding", ["P3"])
        self.assertEqual(n.all()["a1"], {"description": "Beach day", "people": ["P1", "P2"]})
        n.set("a1", "Beach day, edited", [])
        self.assertEqual(n.all()["a1"], {"description": "Beach day, edited", "people": []})
        self.assertEqual(len(n.all()), 2)
        n.set("a1", "", [])                       # empty note removes the record
        self.assertEqual(list(n.all()), ["b2"])
        n.forget("b2"); n.forget("never-existed")
        self.assertEqual(n.all(), {})

    def test_sheet(self):
        fake = FakeSpreadsheet()
        be = SheetBackend(fake, ttl=0)
        n = open_notes(be)
        self.assertIsInstance(n, SheetNotes)
        self.check(n)
        self.assertEqual(fake.worksheet("PhotoNotes").rows[0], ["file_id", "description", "people"])

    def test_text(self):
        d = Path(tempfile.mkdtemp())
        try:
            shutil.copy(SAMPLE, d / "f.txt")
            n = open_notes(TextBackend(d / "f.txt"))
            self.assertIsInstance(n, JsonNotes)
            self.check(n)
        finally:
            shutil.rmtree(d)


class NotesWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp()) / "f.txt"
        shutil.copy(SAMPLE, cls.tmp)
        web.store = FamilyStore(cls.tmp)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); shutil.rmtree(cls.tmp.parent)

    def setUp(self):
        web.configure_auth(None)
        sess = FakeDriveSession(files())
        # read-only on purpose: notes must work without any Drive write access
        web.CAPSULE.update(client=DrivePhotos(sess, "FOLDER", "svc@x.iam"), tried=True)
        Path(self.tmp).with_suffix(".photonotes.json").unlink(missing_ok=True)

    def tearDown(self):
        web.CAPSULE.update(client=None, tried=False)

    def call(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"})
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")

    def test_save_and_list_without_drive_write_access(self):
        self.assertFalse(web.CAPSULE["client"].can_edit)
        st, j = self.call("POST", "/api/capsule/note/a1",
                          {"description": "  Beach   trip with\n grandparents ", "people": ["P1", "P2", "P1"]})
        self.assertEqual(st, 200, j)
        self.assertEqual(j["about"], "Beach trip with grandparents")
        self.assertEqual([p["name"] for p in j["people"]], ["Robert Miller", "Eleanor Miller"])
        photos = {p["id"]: p for p in self.call("GET", "/api/capsule")[1]["photos"]}
        self.assertEqual(photos["a1"]["about"], "Beach trip with grandparents")
        self.assertEqual([p["id"] for p in photos["a1"]["people"]], ["P1", "P2"])
        self.assertEqual(photos["b2"]["about"], "")

    def test_validation(self):
        self.assertEqual(self.call("POST", "/api/capsule/note/zzz", {"description": "x"})[0], 404)
        st, j = self.call("POST", "/api/capsule/note/a1", {"people": ["P999"]})
        self.assertEqual(st, 400); self.assertIn("P999", j["error"])
        st, j = self.call("POST", "/api/capsule/note/a1", {"description": "x" * 5000})
        self.assertEqual(len(j["about"]), 600)

    def test_deleted_person_disappears_from_photo(self):
        self.call("POST", "/api/capsule/note/a1", {"description": "d", "people": ["P11"]})
        self.call("POST", "/api/person/P11/delete", {})
        p = next(x for x in self.call("GET", "/api/capsule")[1]["photos"] if x["id"] == "a1")
        self.assertEqual(p["people"], [])

    def test_chat_can_find_photos_by_person_and_description(self):
        self.call("POST", "/api/capsule/note/a1", {"description": "Picnic by the lake", "people": ["P7"]})
        a = ChatAgent(web.store, FakeClient(), photos_fn=web._photos_for_chat)
        r = a._tool("search_photos", {"query": "picnic"})
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["photos"][0]["people"][0]["name"], "Emily Miller")
        self.assertEqual(a._tool("search_photos", {"person_id": "P7"})["count"], 1)
        self.assertEqual(a._tool("search_photos", {"query": "emily"})["count"], 1)       # matches tagged name
        self.assertEqual(a._tool("search_photos", {"person_id": "P8"})["count"], 0)
        gp = a._tool("get_person", {"person_id": "P7"})
        self.assertEqual(gp["tagged_in_photos"][0]["description"], "Picnic by the lake")
        fc = FakeClient(use("search_photos", person_id="P7"), text("Emily Miller is in one photo: a picnic."))
        out = ChatAgent(web.store, fc, photos_fn=web._photos_for_chat).ask([{"role": "user", "content": "x"}])
        self.assertEqual(out["people"][0]["name"], "Emily Miller")


if __name__ == "__main__":
    unittest.main()
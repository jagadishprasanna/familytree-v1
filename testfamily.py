import base64
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from backends import FamilyError, SheetBackend, TextBackend, records_to_rows
from fake_sheets import FakeSpreadsheet
from family_store import FamilyStore, fit_photo, sniff_image
from migrate_to_sheet import migrate

SAMPLE = Path(__file__).with_name("family.txt")
JPG = b"\xff\xd8\xff\xe0" + b"jpegdata" * 40
PNG = b"\x89PNG\r\n\x1a\n" + b"pngdata!" * 40
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"webpdata" * 40


class Behaviour:
    """Tests shared by every backend. Subclasses provide make() and reopen()."""

    def setUp(self):
        self.s = self.make()

    def test_basic_links(self):
        self.assertEqual({p.id for p in self.s.get_parents("P3")}, {"P1", "P2"})
        self.assertEqual({p.id for p in self.s.get_children("P3")}, {"P7", "P8"})
        self.assertEqual(self.s.get_spouses("P3")[0]["person"].id, "P4")

    def test_siblings(self):
        self.assertEqual([x["person"].id for x in self.s.get_siblings("P3")], ["P5"])

    def test_ancestors_descendants(self):
        self.assertEqual(self.s.ancestors("P10")["P1"], 3)
        self.assertEqual(self.s.descendants("P1")["P10"], 3)

    def test_relationships(self):
        r = self.s.relationship
        self.assertIn("grandfather", r("P7", "P1"))
        self.assertIn("wife", r("P8", "P11"))
        self.assertIn("1st cousin", r("P7", "P9"))
        self.assertIn("nephew", r("P5", "P8"))
        self.assertIn("uncle", r("P9", "P3"))
        self.assertIn("great-grandson", r("P1", "P10"))
        self.assertIn("grandaunt", r("P10", "P5"))
        self.assertIn("grandnephew", r("P5", "P10"))

    def test_add_and_persist(self):
        p = self.s.add_person("Zoe Miller", "F", "2012")
        self.assertEqual(p.id, "P12")
        self.s.add_parent_child("P8", p.id)
        self.s.add_parent_child("P11", p.id)
        again = self.reopen()
        self.assertEqual({x.id for x in again.get_parents("P12")}, {"P8", "P11"})

    def test_add_marriage_persists(self):
        a = self.s.add_person("Ann A", "F")
        b = self.s.add_person("Bob B", "M")
        self.s.add_marriage(a.id, b.id, "2020")
        sp = self.reopen().get_spouses(a.id)
        self.assertEqual((sp[0]["person"].id, sp[0]["start"]), (b.id, "2020"))

    def test_update_person(self):
        self.s.update_person("P7", name="Emily R. Miller", death="2099")
        again = self.reopen()
        self.assertEqual(again.get("P7").name, "Emily R. Miller")
        self.assertEqual(again.get("P7").death, "2099")
        self.assertEqual(again.get("P7").birth, "1980")        # untouched
        again.update_person("P7", death="")                    # clear
        self.assertEqual(self.reopen().get("P7").death, "")
        self.assertEqual({p.id for p in again.get_parents("P7")}, {"P3", "P4"})
        self.assertEqual(len(self.reopen().people), 11)        # no duplicates
        with self.assertRaises(FamilyError):
            again.update_person("P7", name="")
        with self.assertRaises(FamilyError):
            again.update_person("P7", gender="X")
        with self.assertRaises(FamilyError):
            again.update_person("NOPE", name="x")

    def test_validation(self):
        with self.assertRaises(FamilyError):
            self.s.add_parent_child("P10", "P1")        # cycle
        with self.assertRaises(FamilyError):
            self.s.add_parent_child("P7", "P3")         # cycle
        with self.assertRaises(FamilyError):
            self.s.add_marriage("P1", "P3")             # parent/child
        with self.assertRaises(FamilyError):
            self.s.add_person("Bad|Name")
        with self.assertRaises(FamilyError):
            self.s.get("NOPE")

    def test_photo_roundtrip(self):
        self.assertEqual(self.s.get("P7").photo, "")
        self.assertIsNone(self.s.get_photo("P7"))
        p = self.s.set_photo("P7", JPG)
        v1 = p.photo
        self.assertTrue(v1)
        self.assertEqual(self.s.get_photo("P7"), ("image/jpeg", JPG))
        again = self.reopen()                                   # persisted
        self.assertEqual(again.get("P7").photo, v1)
        self.assertEqual(again.get_photo("P7"), ("image/jpeg", JPG))
        self.assertEqual(again.get("P8").photo, "")             # others untouched
        v2 = again.set_photo("P7", PNG).photo                   # replace changes the version
        self.assertNotEqual(v1, v2)
        self.assertEqual(self.reopen().get_photo("P7"), ("image/png", PNG))
        self.assertEqual(self.s.set_photo("P7", WEBP).photo != v2, True)
        self.assertEqual(self.reopen().get_photo("P7")[0], "image/webp")
        self.s.delete_photo("P7")
        self.assertEqual(self.s.get("P7").photo, "")
        self.assertIsNone(self.reopen().get_photo("P7"))
        self.s.delete_photo("P7")                               # deleting twice is fine

    def test_photo_survives_edit(self):
        self.s.set_photo("P7", JPG)
        self.s.update_person("P7", name="Emily Photo")
        self.assertEqual(self.reopen().get_photo("P7"), ("image/jpeg", JPG))
        self.assertEqual(len(self.reopen().people), 11)

    def test_photo_validation(self):
        for bad in (b"hello", b"", b'<svg onload="alert(1)"></svg>', b"GIF89a....", b"%PDF-1.4"):
            with self.assertRaises(FamilyError):
                self.s.set_photo("P7", bad)
        with self.assertRaises(FamilyError):
            self.s.set_photo("P7", JPG + b"x" * self.s.max_photo_bytes)     # too big
        with self.assertRaises(FamilyError):
            self.s.set_photo("NOPE", JPG)
        self.assertEqual(self.s.get("P7").photo, "")

    # ---- nakshatra and gotra
    def test_nakshatra_and_gotra(self):
        p = self.s.add_person("Arjun Rao", "M", "1990", nakshatra="  Rohini ", gotra="Bharadwaja")
        self.assertEqual((p.nakshatra, p.gotra), ("Rohini", "Bharadwaja"))         # tidied
        again = self.reopen()
        self.assertEqual((again.get(p.id).nakshatra, again.get(p.id).gotra), ("Rohini", "Bharadwaja"))
        self.assertEqual((again.get("P7").nakshatra, again.get("P7").gotra), ("", ""))   # others unaffected
        again.update_person(p.id, nakshatra="Pūrva Bhādrapadā")                      # change one, keep the other
        q = self.reopen().get(p.id)
        self.assertEqual((q.nakshatra, q.gotra, q.name, q.birth), ("Pūrva Bhādrapadā", "Bharadwaja", "Arjun Rao", "1990"))
        again.update_person(p.id, name="Arjun R. Rao")                                # other edits keep both
        q = self.reopen().get(p.id)
        self.assertEqual((q.name, q.nakshatra, q.gotra), ("Arjun R. Rao", "Pūrva Bhādrapadā", "Bharadwaja"))
        again.update_person(p.id, gotra="")                                           # clear
        q = self.reopen().get(p.id)
        self.assertEqual((q.nakshatra, q.gotra), ("Pūrva Bhādrapadā", ""))
        self.assertEqual(len(self.reopen().people), 12)

    def test_ailments(self):
        p = self.s.add_person("Rani", "F", "1950", ailments="  Diabetes,\n hypertension ")
        self.assertEqual(p.ailments, "Diabetes, hypertension")
        again = self.reopen()
        self.assertEqual(again.get(p.id).ailments, "Diabetes, hypertension")
        self.assertEqual(again.get("P7").ailments, "")
        again.update_person(p.id, name="Rani Rao")                      # untouched when omitted
        self.assertEqual(again.get(p.id).ailments, "Diabetes, hypertension")
        again.update_person(p.id, ailments="")                          # clear
        self.assertEqual(again.get(p.id).ailments, "")
        with self.assertRaises(FamilyError):
            self.s.add_person("Z", ailments="a|b")
        with self.assertRaises(FamilyError):
            self.s.add_person("Z", ailments="x" * 1001)
        self.assertEqual(len(self.s.add_person("Z", ailments="x" * 1000).ailments), 1000)

    def test_nakshatra_gotra_validation(self):
        with self.assertRaises(FamilyError):
            self.s.add_person("X", nakshatra="a|b")
        self.assertEqual(self.s.add_person("Y", gotra="line\nbreak").gotra, "line break")   # whitespace tidied, harmless
        with self.assertRaises(FamilyError):
            self.s.add_person("X", gotra="g" * 81)
        with self.assertRaises(FamilyError):
            self.s.update_person("P7", nakshatra="n" * 81)
        self.assertEqual(self.s.add_person("Ok", gotra="g" * 80).gotra, "g" * 80)
        self.assertEqual(len(self.reopen().people), 13)

    def test_nakshatra_gotra_survive_photo_delete_and_neighbour_deletion(self):
        self.s.update_person("P8", nakshatra="Ashwini", gotra="Kashyapa")
        self.s.set_photo("P8", JPG)
        self.s.delete_person("P11")
        self.s.delete_photo("P8")
        q = self.reopen().get("P8")
        self.assertEqual((q.nakshatra, q.gotra, q.name), ("Ashwini", "Kashyapa", "James Miller"))

    # ---- deleting people
    def test_delete_person_removes_links_and_keeps_the_rest(self):
        self.s.set_photo("P3", JPG)
        summary = self.s.delete_person("P3")                  # David: 2 parents, 2 children, 1 wife
        self.assertEqual((summary["marriages"], summary["parent_links"], summary["photo"]), (1, 4, True))
        self.assertEqual(summary["person"]["name"], "David Miller")
        for s in (self.s, self.reopen()):                       # in memory and as persisted
            self.assertNotIn("P3", s.people)
            self.assertEqual(len(s.people), 10)
            self.assertEqual([p.id for p in s.get_parents("P7")], ["P4"])      # child keeps the other parent
            self.assertEqual([p.id for p in s.get_children("P1")], ["P5"])     # parent keeps other children
            self.assertEqual(s.get_spouses("P4"), [])
            self.assertEqual(len(s.get_spouses("P5")), 1)                      # unrelated marriage intact
            self.assertIn("mother", s.relationship("P7", "P4"))                # remaining links still work
            self.assertIn("No blood relationship", s.relationship("P10", "P1"))  # David was the bridge
        with self.assertRaises(FamilyError):
            self.s.get_photo("P3")
        with self.assertRaises(FamilyError):
            self.s.delete_person("P3")                          # already gone

    def test_delete_unknown_person(self):
        with self.assertRaises(FamilyError):
            self.s.delete_person("NOPE")
        self.assertEqual(len(self.reopen().people), 11)

    def test_deleted_id_can_be_reused_cleanly(self):
        self.s.set_photo("P11", JPG)
        self.s.delete_person("P11")                             # Rachel: wife of P8, mother of P10
        new = self.s.add_person("Newcomer")
        self.assertEqual(new.id, "P11")                         # id is reused ...
        s = self.reopen()
        self.assertEqual((s.get("P11").photo, s.get_spouses("P11"), s.get_parents("P10")[0].id if s.get_parents("P10") else None),
                         ("", [], "P8"))                        # ... with no ghost links or photo

    def test_delete_everyone_then_start_over(self):
        for pid in list(self.s.people):
            self.s.delete_person(pid)
        s = self.reopen()
        self.assertEqual((s.people, s.marriages, s.parents), ({}, [], {}))
        self.assertEqual(s.add_person("First Again").id, "P1")

    def test_render(self):
        out = self.s.render_descendants("P1")
        self.assertIn("Robert Miller", out)
        self.assertIn("Noah Miller", out)


class TextBackendTests(Behaviour, unittest.TestCase):
    def make(self):
        self.tmp = Path(tempfile.mkdtemp()) / "f.txt"
        shutil.copy(SAMPLE, self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp.parent)
        return FamilyStore(self.tmp)

    def reopen(self):
        return FamilyStore(self.tmp)

    def test_comments_preserved_and_hand_edits_reload(self):
        self.s.update_person("P7", name="New Name")
        text = self.tmp.read_text()
        self.assertTrue(text.startswith("# Sample family data"))
        self.assertEqual(text.count("PERSON | P7 |"), 1)
        with self.tmp.open("a") as fh:
            fh.write("PERSON | P99 | Hand Edited | M | |\n")
        self.assertEqual(self.s.get("P99").name, "Hand Edited")

    def test_old_five_field_lines_still_load_and_upgrade_on_edit(self):
        self.assertEqual((self.s.get("P1").nakshatra, self.s.get("P1").gotra), ("", ""))   # sample has 5 fields
        self.s.update_person("P7", nakshatra="Hasta", gotra="Atri")
        text = self.tmp.read_text()
        self.assertIn("PERSON | P7 | Emily Miller | F | 1980 |  | Hasta | Atri", text)
        self.assertIn("PERSON | P1 | Robert Miller | M | 1925 | 2001", text)           # untouched line stays as it was
        self.assertEqual(self.reopen().get("P7").gotra, "Atri")
        with self.tmp.open("a") as fh:                                                   # hand-typed 7-field line
            fh.write("PERSON | P50 | By Hand | M | 1970 | | Magha | Vasishtha\n")
        self.assertEqual((self.s.get("P50").nakshatra, self.s.get("P50").gotra), ("Magha", "Vasishtha"))

    def test_photo_files_and_hand_added_photo(self):
        self.s.set_photo("P7", JPG)
        self.assertTrue((self.tmp.parent / "f_photos" / "P7.jpg").exists())
        self.s.set_photo("P7", PNG)                              # old extension removed
        self.assertEqual(sorted(p.name for p in (self.tmp.parent / "f_photos").iterdir()), ["P7.png"])
        (self.tmp.parent / "f_photos" / "P8.jpg").write_bytes(JPG)   # dropped in by hand
        self.assertTrue(self.s.get("P8").photo)

    def test_delete_keeps_comments_and_removes_photo_file(self):
        self.s.set_photo("P7", JPG)
        self.s.delete_person("P7")
        text = self.tmp.read_text()
        self.assertTrue(text.startswith("# Sample family data"))
        self.assertNotIn("P7", text.replace("# ", ""))
        self.assertFalse((self.tmp.parent / "f_photos" / "P7.jpg").exists())
        self.assertIn("PERSON | P8 |", text)

    def test_photo_path_traversal_blocked(self):
        evil = self.s.add_person("Evil", person_id="../evil")
        with self.assertRaises(FamilyError):
            self.s.set_photo(evil.id, JPG)
        self.assertFalse(list(self.tmp.parent.glob("evil*")))


class SheetBackendTests(Behaviour, unittest.TestCase):
    def make(self):
        self.fake = FakeSpreadsheet()
        text = TextBackend(self._sample_copy())
        SheetBackend(self.fake, ttl=0).replace_all(records_to_rows(text.read()))
        return FamilyStore(SheetBackend(self.fake, ttl=0))

    def _sample_copy(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        shutil.copy(SAMPLE, d / "f.txt")
        return d / "f.txt"

    def reopen(self):
        return FamilyStore(SheetBackend(self.fake, ttl=0))

    def test_tabs_and_headers_created(self):
        fake = FakeSpreadsheet()
        FamilyStore(SheetBackend(fake, ttl=0))
        self.assertEqual(fake.worksheet("People").rows[0],
                         ["id", "name", "gender", "birth", "death", "nakshatra", "gotra", "ailments"])
        self.assertEqual(fake.worksheet("Marriages").rows[0], ["person_a", "person_b", "start", "end"])
        self.assertEqual(fake.worksheet("Parents").rows[0], ["parent_id", "child_id"])

    def test_existing_five_column_sheet_is_upgraded(self):
        fake = FakeSpreadsheet()
        ws = fake.add_worksheet("People", 100, 5)                      # a sheet created before these fields existed
        ws.rows = [["id", "name", "gender", "birth", "death"], ["P1", "Old Row", "M", "1950", ""]]
        s = FamilyStore(SheetBackend(fake, ttl=0))
        self.assertEqual(ws.rows[0], ["id", "name", "gender", "birth", "death", "nakshatra", "gotra", "ailments"])
        self.assertEqual((s.get("P1").name, s.get("P1").nakshatra), ("Old Row", ""))
        s.update_person("P1", nakshatra="Rohini", gotra="Gautama")
        self.assertEqual(ws.rows[1], ["P1", "Old Row", "M", "1950", "", "Rohini", "Gautama", ""])
        self.assertEqual(len(ws.rows), 2)

    def test_custom_header_is_left_alone(self):
        fake = FakeSpreadsheet()
        ws = fake.add_worksheet("People", 100, 5)
        ws.rows = [["ID", "Full name", "Sex", "Born", "Died"], ["P1", "Custom", "M", "", ""]]
        FamilyStore(SheetBackend(fake, ttl=0))
        self.assertEqual(ws.rows[0], ["ID", "Full name", "Sex", "Born", "Died"])

    def test_hand_edit_in_sheet_is_seen(self):
        self.fake.worksheet("People").rows.append(["P99", "Typed In Sheet", "M", "1990", ""])
        self.assertEqual(self.s.get("P99").name, "Typed In Sheet")

    def test_bad_reference_names_the_row(self):
        self.fake.worksheet("Parents").rows.append(["P1", "P404"])
        with self.assertRaises(FamilyError) as cm:
            FamilyStore(SheetBackend(self.fake, ttl=0))
        self.assertIn("Parents row", str(cm.exception))
        self.assertIn("P404", str(cm.exception))

    def test_blank_rows_and_comments_ignored(self):
        ws = self.fake.worksheet("People")
        ws.rows.insert(3, [])                    # blank row in the middle
        ws.rows.append(["# a note", "", "", "", ""])
        s = self.reopen()
        self.assertEqual(len(s.people), 11)
        s.update_person("P7", name="Still Right Row")   # row numbers must survive the blank row
        self.assertEqual(self.reopen().get("P7").name, "Still Right Row")
        self.assertEqual(self.reopen().get("P8").name, "James Miller")

    def test_cache_limits_reads(self):
        s = FamilyStore(SheetBackend(self.fake, ttl=60))
        before = self.fake.read_calls
        for _ in range(20):
            s.get_children("P3"); s.relationship("P7", "P1")
        self.assertEqual(self.fake.read_calls, before)

    def test_migrate(self):
        fake = FakeSpreadsheet()
        counts = migrate(str(self._sample_copy()), SheetBackend(fake, ttl=0))
        self.assertEqual(counts, {"PERSON": 11, "MARRIED": 4, "PARENT": 12, "PHOTO": 0})
        self.assertEqual(len(FamilyStore(SheetBackend(fake, ttl=0)).people), 11)
        with self.assertRaises(FamilyError):                       # won't clobber
            migrate(str(self._sample_copy()), SheetBackend(fake, ttl=0))
        migrate(str(self._sample_copy()), SheetBackend(fake, ttl=0), force=True)

    # ---- photos in the Photos tab
    def test_photos_tab_layout(self):
        self.s.set_photo("P7", JPG)
        rows = self.fake.worksheet("Photos").rows
        self.assertEqual(rows[0], ["person_id", "mime", "version", "data"])
        self.assertEqual(rows[1][:2], ["P7", "image/jpeg"])
        self.assertEqual(base64.b64decode(rows[1][3]), JPG)
        self.s.set_photo("P7", PNG)
        self.assertEqual(len(self.fake.worksheet("Photos").rows), 2)      # replaced in place
        self.s.delete_photo("P7")
        self.assertEqual(len(self.fake.worksheet("Photos").rows), 1)      # row removed

    def test_photos_fetched_in_one_request(self):
        for pid in ("P1", "P2", "P3", "P4", "P5"):
            self.s.set_photo(pid, JPG + pid.encode())
        s = FamilyStore(SheetBackend(self.fake, ttl=60))                  # fresh instance, cold cache
        before = self.fake.read_calls
        for pid in ("P1", "P2", "P3", "P4", "P5"):
            self.assertEqual(s.get_photo(pid)[1], JPG + pid.encode())
        self.assertEqual(self.fake.read_calls - before, 1)

    def test_photo_changed_elsewhere_is_picked_up(self):
        self.s.set_photo("P7", JPG)
        other = FamilyStore(SheetBackend(self.fake, ttl=0))               # another server instance
        other.set_photo("P7", PNG)
        self.assertEqual(self.s.get_photo("P7"), ("image/png", PNG))

    def test_orphan_photo_row_ignored_and_replace_all_keeps_photos(self):
        self.s.set_photo("P7", JPG)
        self.fake.worksheet("Photos").rows.append(["GONE", "image/jpeg", "abc", base64.b64encode(JPG).decode()])
        s = self.reopen()
        self.assertEqual(s.get("P7").photo != "", True)
        self.assertNotIn("GONE", s.people)
        migrate(str(self._sample_copy()), SheetBackend(self.fake, ttl=0), force=True)
        self.assertEqual(self.reopen().get_photo("P7"), ("image/jpeg", JPG))

    def test_sheet_size_limit(self):
        self.assertEqual(self.s.max_photo_bytes, 33_000)
        self.s.set_photo("P7", JPG + b"x" * (33_000 - len(JPG)))          # exactly at the limit: ok
        with self.assertRaises(FamilyError):
            self.s.set_photo("P8", JPG + b"x" * 33_000)

    def test_delete_removes_rows_from_every_tab(self):
        self.s.set_photo("P3", JPG)
        before = {n: len(self.fake.worksheet(n).rows) for n in ("People", "Marriages", "Parents", "Photos")}
        self.s.delete_person("P3")
        after = {n: len(self.fake.worksheet(n).rows) for n in before}
        self.assertEqual({n: before[n] - after[n] for n in before},
                         {"People": 1, "Marriages": 1, "Parents": 4, "Photos": 1})     # rows removed, not blanked
        for name in ("People", "Marriages", "Parents", "Photos"):
            for row in self.fake.worksheet(name).rows[1:]:
                self.assertNotIn("P3", row[:2], name)

    def test_delete_is_one_batch_and_all_or_nothing(self):
        calls = []
        real = self.fake.batch_update
        self.fake.batch_update = lambda body: (calls.append(body), real(body))[1]
        self.s.delete_person("P11")
        self.assertEqual(len(calls), 1)                          # every row removal in a single request

        snapshot = {n: [list(r) for r in self.fake.worksheet(n).rows] for n in ("People", "Marriages", "Parents")}
        def bad_batch(body):                                     # last request is invalid -> whole batch must fail
            bad = {"deleteDimension": {"range": {"sheetId": 99999, "dimension": "ROWS", "startIndex": 1, "endIndex": 2}}}
            return real({"requests": body["requests"] + [bad]})
        self.fake.batch_update = bad_batch
        with self.assertRaises(FamilyError):
            self.s.delete_person("P3")
        self.fake.batch_update = real
        for n, rows in snapshot.items():
            self.assertEqual(self.fake.worksheet(n).rows, rows, n)      # nothing half-deleted
        self.assertEqual(len(self.reopen().people), 10)

    def test_frozen_header_error_is_explained(self):
        def frozen(body):
            raise RuntimeError("You can't delete all the non-frozen rows on a sheet.")
        self.fake.batch_update = frozen
        with self.assertRaises(FamilyError) as cm:
            self.s.delete_person("P3")
        self.assertIn("Freeze", str(cm.exception))

    def test_migrate_copies_photos(self):
        src = self._sample_copy()
        (src.parent / "f_photos").mkdir()
        (src.parent / "f_photos" / "P3.jpg").write_bytes(JPG)
        (src.parent / "f_photos" / "P4.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 40_000)  # too big
        fake = FakeSpreadsheet()
        counts = migrate(str(src), SheetBackend(fake, ttl=0))
        self.assertEqual(counts["PHOTO"], 1)
        self.assertEqual(FamilyStore(SheetBackend(fake, ttl=0)).get_photo("P3"), ("image/jpeg", JPG))


class FitPhotoTests(unittest.TestCase):
    def test_sniff(self):
        self.assertEqual((sniff_image(JPG), sniff_image(PNG), sniff_image(WEBP)),
                         ("image/jpeg", "image/png", "image/webp"))

    def test_small_photo_unchanged(self):
        self.assertEqual(fit_photo(JPG, 33_000), JPG)

    def test_big_photo_shrunk_to_fit(self):
        try:
            import io
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        img = Image.frombytes("RGB", (1200, 900), os.urandom(1200 * 900 * 3))   # noisy = hard to compress
        buf = io.BytesIO(); img.save(buf, "PNG")
        out = fit_photo(buf.getvalue(), 33_000)
        self.assertLessEqual(len(out), 33_000)
        self.assertEqual(sniff_image(out), "image/jpeg")
        w, h = Image.open(io.BytesIO(out)).size
        self.assertEqual(w, h)                                                  # square crop
        with self.assertRaises(FamilyError):
            fit_photo(b"not an image" * 10_000, 33_000)


if __name__ == "__main__":
    unittest.main()
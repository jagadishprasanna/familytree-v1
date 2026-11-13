import shutil
import tempfile
import unittest
from pathlib import Path

from backends import FamilyError, SheetBackend, TextBackend, records_to_rows
from fake_sheets import FakeSpreadsheet
from family_store import FamilyStore
from migrate_to_sheet import migrate

SAMPLE = Path(__file__).with_name("family.txt")


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
        self.assertEqual(fake.worksheet("People").rows[0], ["id", "name", "gender", "birth", "death"])
        self.assertEqual(fake.worksheet("Marriages").rows[0], ["person_a", "person_b", "start", "end"])
        self.assertEqual(fake.worksheet("Parents").rows[0], ["parent_id", "child_id"])

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
        self.assertEqual(counts, {"PERSON": 11, "MARRIED": 4, "PARENT": 12})
        self.assertEqual(len(FamilyStore(SheetBackend(fake, ttl=0)).people), 11)
        with self.assertRaises(FamilyError):                       # won't clobber
            migrate(str(self._sample_copy()), SheetBackend(fake, ttl=0))
        migrate(str(self._sample_copy()), SheetBackend(fake, ttl=0), force=True)


if __name__ == "__main__":
    unittest.main()
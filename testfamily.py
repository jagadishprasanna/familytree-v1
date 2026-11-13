import shutil
import tempfile
import unittest
from pathlib import Path

from family_store import FamilyError, FamilyStore

SAMPLE = Path(__file__).with_name("family.txt")


class FamilyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "f.txt"
        shutil.copy(SAMPLE, self.tmp)
        self.s = FamilyStore(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp.parent)

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

    def test_add_and_persist(self):
        p = self.s.add_person("Zoe Miller", "F", "2012")
        self.assertEqual(p.id, "P12")
        self.s.add_parent_child("P8", p.id)
        self.s.add_parent_child("P11", p.id)
        again = FamilyStore(self.tmp)
        self.assertEqual({x.id for x in again.get_parents("P12")}, {"P8", "P11"})
        self.assertTrue(self.tmp.read_text().startswith("# Sample family data"))

    def test_validation(self):
        with self.assertRaises(FamilyError):
            self.s.add_parent_child("P10", "P1")        # cycle
        with self.assertRaises(FamilyError):
            self.s.add_parent_child("P7", "P3")         # three parents / cycle
        with self.assertRaises(FamilyError):
            self.s.add_marriage("P1", "P3")             # parent/child
        with self.assertRaises(FamilyError):
            self.s.add_person("Bad|Name")
        with self.assertRaises(FamilyError):
            self.s.get("NOPE")

    def test_manual_edit_reload(self):
        with self.tmp.open("a") as fh:
            fh.write("PERSON | P99 | Hand Edited | M | |\n")
        self.assertEqual(self.s.get("P99").name, "Hand Edited")

    def test_render(self):
        out = self.s.render_descendants("P1")
        self.assertIn("Robert Miller", out)
        self.assertIn("Noah Miller", out)


if __name__ == "__main__":
    unittest.main()
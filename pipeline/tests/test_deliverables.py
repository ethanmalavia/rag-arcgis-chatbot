"""Semantic validation of the committed data deliverables.

Replaces the byte-exact "rebuild + git diff" build-guard, which was fragile:
the pipeline is not bit-reproducible off its runner (geocode/OCR), so a byte
diff flapped red even when nothing was wrong. These checks instead assert the
invariants that actually matter — schema, referential integrity, value ranges,
and geocode sanity — on whatever data is committed. They are fast (no rebuild,
no network) and deterministic.
"""
from __future__ import annotations

import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "pipeline"
sys.path.insert(0, str(PIPELINE))

from eaglegis.config import CANONICAL_LOCATION_TYPES

CORE = ROOT / "backend" / "data" / "silver" / "core"
REVIEW = ROOT / "backend" / "data" / "silver" / "review"
GOLD = ROOT / "backend" / "data" / "gold"


def load(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


class SchemaTests(unittest.TestCase):
    def test_gold_has_project_grouping_columns(self):
        cols = set(load(GOLD / "meetings_ai_public.csv")[0].keys())
        for required in ("RecordId", "ItemId", "ProjectName", "ProjectId",
                         "ProjectKey", "MeetingDate", "Summary", "CitationText"):
            self.assertIn(required, cols)

    def test_core_tables_have_expected_keys(self):
        expected = {
            "projects.csv": {"project_id", "project_name"},
            "agenda_categories.csv": {"category_id", "category_name"},
            "agenda_items.csv": {"item_id", "meeting_id", "category_id"},
            "agenda_item_projects.csv": {"item_id", "project_id"},
            "meetings.csv": {"meeting_id", "board_id", "meeting_date"},
        }
        for name, keys in expected.items():
            cols = set(load(CORE / name)[0].keys())
            self.assertTrue(keys <= cols, f"{name} missing {keys - cols}")


class ReferentialIntegrityTests(unittest.TestCase):
    def test_item_project_links_resolve(self):
        projects = {r["project_id"] for r in load(CORE / "projects.csv")}
        items = {r["item_id"] for r in load(CORE / "agenda_items.csv")}
        for link in load(CORE / "agenda_item_projects.csv"):
            self.assertIn(link["project_id"], projects)
            self.assertIn(link["item_id"], items)

    def test_agenda_items_reference_valid_meeting_and_category(self):
        meetings = {r["meeting_id"] for r in load(CORE / "meetings.csv")}
        categories = {r["category_id"] for r in load(CORE / "agenda_categories.csv")}
        for item in load(CORE / "agenda_items.csv"):
            self.assertIn(item["meeting_id"], meetings)
            self.assertIn(item["category_id"], categories)

    def test_gold_project_ids_resolve(self):
        projects = {r["project_id"] for r in load(CORE / "projects.csv")}
        for row in load(GOLD / "meetings_ai_public.csv"):
            pid = row["ProjectId"].strip()
            if pid:
                self.assertIn(pid, projects)


class ValueInvariantTests(unittest.TestCase):
    def test_category_ids_are_the_approved_taxonomy(self):
        ids = {int(r["category_id"]) for r in load(CORE / "agenda_categories.csv")}
        self.assertTrue(ids <= set(range(1, 9)), f"unexpected category_id: {ids}")

    def test_location_types_are_canonical(self):
        offenders = {
            r["location_type"]
            for r in load(CORE / "locations.csv")
            if r["location_type"] and r["location_type"] not in CANONICAL_LOCATION_TYPES
        }
        self.assertEqual(offenders, set(), f"non-canonical location_type: {offenders}")

    def test_coordinates_are_plausible_for_the_region(self):
        for r in load(CORE / "locations.csv"):
            lat, lon = r.get("latitude", ""), r.get("longitude", "")
            if lat and lon:
                self.assertTrue(24.0 <= float(lat) <= 28.0, f"lat {lat}")
                self.assertTrue(-83.0 <= float(lon) <= -80.0, f"lon {lon}")


class GeocodeSanityTests(unittest.TestCase):
    def test_no_mismatch_rows(self):
        rows = load(REVIEW / "location_verification.csv")
        bad = [r for r in rows if r.get("Status") == "MISMATCH"]
        self.assertEqual(bad, [], f"{len(bad)} MISMATCH geocode row(s)")


class MapLayerTests(unittest.TestCase):
    def test_layer_files_partition_the_map_data(self):
        main = load(GOLD / "arcgis" / "arcgis_agenda_map_data.csv")
        by_cat: dict[str, int] = {}
        for r in main:
            by_cat[r["CategoryID"]] = by_cat.get(r["CategoryID"], 0) + 1
        layer_total = 0
        for layer in (GOLD / "arcgis" / "layers").glob("*.csv"):
            rows = load(layer)
            cats = {r["CategoryID"] for r in rows}
            self.assertLessEqual(len(cats), 1, f"{layer.name} mixes categories {cats}")
            if cats:
                cat = next(iter(cats))
                self.assertEqual(len(rows), by_cat.get(cat), f"{layer.name} count")
            layer_total += len(rows)
        self.assertEqual(layer_total, len(main), "layers do not partition the map data")


class SanityFloorTests(unittest.TestCase):
    def test_deliverables_are_non_trivially_populated(self):
        # guards against a build that silently produced near-empty output
        self.assertGreater(len(load(CORE / "agenda_items.csv")), 500)
        self.assertGreater(len(load(CORE / "meetings.csv")), 50)
        self.assertGreater(len(load(GOLD / "meetings_ai_public.csv")), 500)


if __name__ == "__main__":
    unittest.main()

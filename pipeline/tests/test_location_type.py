from __future__ import annotations

import csv
import sys
import unittest
from pathlib import Path

PIPELINE = Path(__file__).resolve().parents[1]
ROOT = PIPELINE.parent
sys.path.insert(0, str(PIPELINE))

from eaglegis.config import CANONICAL_LOCATION_TYPES, normalize_location_type


class NormalizeLocationTypeTests(unittest.TestCase):
    def test_canonical_values_pass_through(self):
        for value in CANONICAL_LOCATION_TYPES:
            self.assertEqual(normalize_location_type(value), value)

    def test_legacy_labels_fold_to_canonical(self):
        cases = {
            "Project Site": "PROJECT_SITE",
            "General Area": "GENERAL_AREA",
            "Development": "DEVELOPMENT",
            "Infrastructure": "INFRASTRUCTURE",
            "Meeting Venue": "NAMED_VENUE",
            "Road": "ROAD",
            "Trail": "TRAIL",
            "Park": "PARK",
            "Neighborhood": "NEIGHBORHOOD",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_location_type(raw), expected)
            self.assertIn(normalize_location_type(raw), CANONICAL_LOCATION_TYPES)

    def test_blank_and_none_yield_empty(self):
        self.assertEqual(normalize_location_type(None), "")
        self.assertEqual(normalize_location_type("   "), "")

    def test_unknown_label_is_upper_snaked(self):
        self.assertEqual(normalize_location_type("some new label"), "SOME_NEW_LABEL")

    def test_committed_locations_are_all_canonical(self):
        path = ROOT / "backend" / "data" / "silver" / "core" / "locations.csv"
        with path.open(encoding="utf-8", newline="") as fh:
            values = {row["location_type"] for row in csv.DictReader(fh)}
        offenders = {v for v in values if v and v not in CANONICAL_LOCATION_TYPES}
        self.assertEqual(offenders, set(), f"non-canonical location_type in data: {offenders}")


if __name__ == "__main__":
    unittest.main()

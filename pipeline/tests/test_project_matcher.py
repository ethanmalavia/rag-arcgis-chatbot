"""Structured road-project matching: a road name alone must not capture
private developments on that road (require road + works, exclude land-use)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PIPELINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE))

from eaglegis.classifiers import match_projects


class CorkscrewRoadProjectTests(unittest.TestCase):
    def test_public_works_actions_match(self):
        for text in (
            "Corkscrew Road Path, Landscaping & Lighting Project Construction Manager at Risk",
            "Corkscrew Road/Puente Lane Traffic Signal Interlocal Agreement",
            "Corkscrew Road Easement Acquisition Services Contract",
            "Corkscrew Road Widening Update",
        ):
            self.assertIn("Corkscrew Road", match_projects(text), text)

    def test_private_developments_on_the_road_do_not_match(self):
        for text in (
            "Via Coconut - Zoning Amendment (District 4) 8990 Corkscrew Road",
            "Celebree School (District 4) 10351 Corkscrew Commons Drive",
            "Goodwill Industries - Development Order (DOS2023-E005) Corkscrew Road",
            "Cell Phone Tower - Bella Terra (SEZ2023-E001) near Corkscrew Road",
        ):
            self.assertNotIn("Corkscrew Road", match_projects(text), text)

    def test_road_name_without_a_works_action_does_not_match(self):
        # a bare mention with no public-works action is not the road project
        self.assertNotIn(
            "Corkscrew Road", match_projects("Tour of a property at 8801 Corkscrew Road")
        )

    def test_other_road_projects_use_the_same_structure(self):
        works = {
            "Williams Road Widening Update": "Williams Road Improvements",
            "Sandy Lane Bike/Ped Improvements Construction Manager at Risk Contract":
                "Sandy Lane Improvements",
            "Estero Parkway Roadway and Landscape Design": "Estero Parkway Improvements",
            "Ben Hill Griffin Parkway Improvements Design and Permitting Contracts":
                "Ben Hill Griffin Parkway Improvements",
        }
        for text, project in works.items():
            self.assertIn(project, match_projects(text), text)

    def test_developments_on_other_roads_do_not_match(self):
        cases = [
            ("Village Initiated Rezoning - 9000 Williams Road Property (DCI2022-E006)",
             "Williams Road Improvements"),
            ("Walmart Expansion (DOS2023-E008) near Estero Parkway",
             "Estero Parkway Improvements"),
            ("Chick-Fil-A - Development Order near Ben Hill Griffin Parkway",
             "Ben Hill Griffin Parkway Improvements"),
            ("Mayfield Village - Residential Planned Development Rezoning off Sandy Lane",
             "Sandy Lane Improvements"),
        ]
        for text, project in cases:
            self.assertNotIn(project, match_projects(text), text)

    def test_list_form_projects_still_work(self):
        # legacy list-form aliases are unaffected by the structured form
        self.assertIn("BERT Rail Trail", match_projects("update on the rail trail segment"))


if __name__ == "__main__":
    unittest.main()

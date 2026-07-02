from __future__ import annotations

import sys
import unittest
from pathlib import Path
from uuid import uuid4

PIPELINE = Path(__file__).resolve().parents[1]
ROOT = PIPELINE.parent
sys.path.insert(0, str(PIPELINE))

from build import (
    NormalizedBuilder,
    ensure_estero_address,
    infer_application_id,
    infer_vote_counts,
    normalize_address_candidate,
    should_suppress_arcgis_item,
)
from eaglegis.classifiers import (
    extract_address_candidates,
    infer_action_type,
    infer_category,
    match_locations,
)
from eaglegis.extractors import (
    extract_agenda_entries,
    extract_date,
    infer_meeting_type,
    parse_date,
    raw_pdf_url,
)
from eaglegis.sources import PdfAsset, iter_local_pdfs


class PipelineParserTests(unittest.TestCase):
    def test_application_id_ignores_common_word_fragments(self) -> None:
        self.assertIsNone(infer_application_id("Approved with staff conditions."))
        self.assertIsNone(infer_application_id("Standards for elevated single-family homes."))
        self.assertIsNone(infer_application_id("The site plan was discussed."))

    def test_application_id_extracts_pzdb_identifiers(self) -> None:
        self.assertEqual(
            infer_application_id("Development Order (DOS2023-E012) with staff conditions."),
            "DOS2023-E012",
        )
        self.assertEqual(
            infer_application_id("Limited Development Order LDO2024-E041 was approved."),
            "LDO2024-E041",
        )
        self.assertEqual(
            infer_application_id("Outdoor Consumption on Premises COP2023-E002."),
            "COP2023-E002",
        )

    def test_fallback_approved_does_not_capture_board_header(self) -> None:
        text = (
            "APPROVED BY BOARD FEBRUARY 13, 2024 Planning Zoning and Design Board Meeting "
            "Village of Estero 9401 Corkscrew Palms Circle Estero, FL 33928 "
            "1. CALL TO ORDER 2. ROLL CALL"
        )
        self.assertEqual(extract_agenda_entries(text), [])

    def test_action_entry_preserves_following_vote_text(self) -> None:
        text = (
            "Motion: Motion to approve. Motion by: Board Member Jones Seconded by: Board Member Wallace "
            "Action: Approved the Development Order with staff conditions. "
            "Vote: Aye: Board Members Jones, Wallace Nay: None Abstentions: None "
            "Public Input None."
        )
        entries = extract_agenda_entries(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].action_text, "Approved the Development Order with staff conditions")
        self.assertIn("Aye: Board Members Jones, Wallace", entries[0].vote_text or "")

    def test_action_without_vote_stops_at_next_numbered_heading(self) -> None:
        text = (
            "Motion: Motion to continue. Action: Approved to continue the meeting until 9:15 pm. "
            "7. WORKSHOP (1) Culver's Coconut Point Development Order (District 6) "
            "8400 Murano Del Lago Dr, east of US 41."
        )
        entries = extract_agenda_entries(text)
        self.assertEqual(entries[0].action_text, "Approved to continue the meeting until 9:15 pm")

    def test_action_without_vote_stops_at_parenthetical_heading(self) -> None:
        text = (
            "Action: Appointed Michael Sheeley as Vice Chairman. "
            "(a) Aldi - Development Order (Pending Submittal) (District 5) Located east of Stoneybrook."
        )
        entries = extract_agenda_entries(text)
        self.assertEqual(entries[0].action_text, "Appointed Michael Sheeley as Vice Chairman")

    def test_section_fallback_extracts_non_action_location_item(self) -> None:
        text = (
            "PUBLIC INFORMATION MEETINGS (a) Estero Storage LLC - Zoning Amendment "
            "(No application submitted) (District 4) 10251 Arcos Avenue located on a 7-acre "
            "vacant site east of the Estero Medical Center. Staff Presentation/Comments Mary Gibbs. "
            "BOARD COMMUNICATIONS Next meeting."
        )
        entries = extract_agenda_entries(text)
        self.assertEqual(len(entries), 1)
        self.assertIn("No formal action recorded", entries[0].action_text)
        self.assertIn("10251 Arcos Avenue", entries[0].action_text)

    def test_address_candidates_include_estero_street_addresses(self) -> None:
        text = (
            "Culver's Coconut Point Development Order at 8400 Murano Del Lago Dr, "
            "east of US 41 and south of Pelican Colony Blvd."
        )
        self.assertIn("8400 Murano Del Lago Dr", extract_address_candidates(text))

    def test_address_candidates_include_tamiami_trail(self) -> None:
        text = "19701 S. Tamiami Trail, located east on US 41, north of the Vines entrance."
        self.assertIn("19701 S. Tamiami Trail", extract_address_candidates(text))

    def test_address_candidate_normalizes_ocr_dropped_digit(self) -> None:
        self.assertEqual(normalize_address_candidate("0251 Arcos Avenue"), "10251 Arcos Avenue")

    def test_ensure_estero_address_does_not_treat_street_name_as_city(self) -> None:
        self.assertEqual(
            ensure_estero_address("9170 Estero River Ct"),
            "9170 Estero River Ct, Estero, FL",
        )

    def test_vote_count_parses_aye_nay_abstentions(self) -> None:
        yes, no, abstain = infer_vote_counts(
            "Vote: Aye: Board Members Jones, Jeannin, Naratil, Chairman Wood "
            "Nay: Board Member Wallace Abstentions: None"
        )
        self.assertEqual(yes, 4)
        self.assertEqual(no, 1)
        self.assertEqual(abstain, 0)

    def test_empty_abstentions_stop_at_numbered_heading(self) -> None:
        yes, no, abstain = infer_vote_counts(
            "Vote: Aye: Board Members Jones, Jeannin Nay: Board Member Wallace "
            "Abstentions: 8. PUBLIC INPUT None."
        )
        self.assertEqual(yes, 2)
        self.assertEqual(no, 1)
        self.assertEqual(abstain, 0)

    def test_empty_abstentions_stop_at_parenthetical_heading(self) -> None:
        yes, no, abstain = infer_vote_counts(
            "Vote: Aye: Board Members Jones, Jeannin Nay: Abstentions: "
            "(b) Development Order - Arcos Executive Office (DOS2017-E006)"
        )
        self.assertEqual(yes, 2)
        self.assertEqual(no, 0)
        self.assertEqual(abstain, 0)

    def test_empty_abstentions_stop_at_page_header(self) -> None:
        yes, no, abstain = infer_vote_counts(
            "Vote: Aye: Board Members Jones, Jeannin Nay: Board Member Wallace "
            "Abstentions: Planning Zoning and Design Board Minutes - July 11, 2023 Page 5 of 6"
        )
        self.assertEqual(yes, 2)
        self.assertEqual(no, 1)
        self.assertEqual(abstain, 0)

    def test_local_pdf_discovery_is_recursive(self) -> None:
        root = ROOT / ".test_tmp" / uuid4().hex
        try:
            nested = root / "2024"
            nested.mkdir(parents=True)
            pdf_path = nested / "20240213 PZDB Minutes.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            assets = iter_local_pdfs(root)
            self.assertEqual([asset.filename for asset in assets], ["20240213 PZDB Minutes.pdf"])
        finally:
            if root.exists():
                for path in sorted(root.rglob("*"), reverse=True):
                    if path.is_file():
                        path.unlink()
                    else:
                        path.rmdir()
                root.rmdir()

    def test_builder_skips_exact_duplicate_agenda_items(self) -> None:
        builder = NormalizedBuilder(source_rows=[])
        kwargs = {
            "meeting_id": 1,
            "item_order": 1,
            "meeting_type": "Planning Zoning & Design Board",
            "item_title": "Development Order (DOS2024-E001)",
            "action_text": "Approved the Development Order with staff conditions",
            "vote_text": None,
            "staff_code": None,
            "needs_ocr": False,
            "date_missing": False,
            "used_csv_fallback": False,
            "fallback_projects": [],
            "fallback_locations": [],
            "asset": PdfAsset(path="test.pdf", filename="test.pdf", data=b""),
        }
        builder._add_action(**kwargs)
        builder._add_action(**kwargs)
        self.assertEqual(len(builder.agenda_items), 1)

    def test_location_extraction_uses_agenda_title_context(self) -> None:
        builder = NormalizedBuilder(source_rows=[])
        builder._add_action(
            meeting_id=1,
            item_order=1,
            meeting_type="Planning Zoning & Design Board",
            item_title=(
                "Estero Townhomes EPD - Rezoning (DCI2024-E003) (District 4) "
                "21.4 acres located on the northeast corner of Corkscrew Road and Sandy Lane"
            ),
            action_text="Approved Development Order with staff stipulations.",
            vote_text="Aye: Board Members Jones Nay: Abstentions:",
            staff_code=None,
            needs_ocr=False,
            date_missing=False,
            used_csv_fallback=False,
            fallback_projects=[],
            fallback_locations=[],
            asset=PdfAsset(path="test.pdf", filename="test.pdf", data=b""),
        )

        self.assertEqual(
            builder.agenda_items[0]["address_raw"],
            "Estero Townhomes EPD site, 9301 Corkscrew Road, Estero, FL",
        )
        self.assertEqual(len(builder.locations_v2), 1)
        self.assertEqual(builder.locations_v2[0]["geocode_confidence"], 1.0)

    def test_council_text_override_promotes_bella_terra_site_address(self) -> None:
        builder = NormalizedBuilder(source_rows=[])
        builder._add_action(
            meeting_id=1,
            item_order=1,
            meeting_type="Village Council Regular Meeting",
            item_title=(
                "Ordinance No. 2024-05 Bella Terra Cell Tower "
                "Approving an Amendment to the Commercial Planned Development Zoning "
                "to Allow the use of Wireless Communication Facility"
            ),
            action_text="Passed first reading and set public hearing and second reading for April 17, 2024.",
            vote_text=None,
            staff_code=None,
            needs_ocr=False,
            date_missing=False,
            used_csv_fallback=False,
            fallback_projects=[],
            fallback_locations=[],
            asset=PdfAsset(path="test.pdf", filename="test.pdf", data=b""),
        )

        self.assertEqual(
            builder.agenda_items[0]["address_raw"],
            "19980 Bella Terra Boulevard, Estero, FL",
        )
        self.assertEqual(len(builder.locations_v2), 1)
        self.assertEqual(builder.locations_v2[0]["latitude"], 26.450582796918)
        self.assertEqual(builder.locations_v2[0]["longitude"], -81.73115952021)

    def test_long_public_comment_context_keeps_agenda_heading(self) -> None:
        text = (
            "(a) Estero Townhomes EPD - Rezoning (DCI2024-E003) (District 4) "
            "21.4 acres located on the northeast corner of Corkscrew Road and Sandy Lane. "
            "Staff Presentation/Comments Mary Gibbs. Public Comment "
            + " ".join(f"Speaker {i}, Estero" for i in range(180))
            + " Board Questions or Comments Board Members. Motion: Motion to approve. "
            "Motion by: Board Member Jones Seconded by: Board Member Wallace "
            "Action: Approved Development Order with staff stipulations. "
            "Vote: Aye: Board Members Jones and Wallace Nay: Abstentions:"
        )

        entries = extract_agenda_entries(text)

        self.assertEqual(len(entries), 1)
        self.assertIn("Corkscrew Road", entries[0].title)

    def test_us_41_sentence_does_not_split_agenda_title(self) -> None:
        text = (
            "(c) 8111 Broadway East Development Order Amendment #1 (DOS2019-E004) "
            "(District 4) 8111 Broadway East is a 1-acre site located 200 feet east of US 41. "
            "It was developed in 1983 by the U.S. Federal Government for a Post Office. "
            "Staff Presentation/Comments Mary Gibbs. Motion: Motion to approve. "
            "Motion by: Board Member Jones Seconded by: Board Member Wallace "
            "Action: Approved Development Order with conditions. "
            "Vote: Aye: Board Members Jones and Wallace Nay: Abstentions:"
        )

        entries = extract_agenda_entries(text)

        self.assertEqual(len(entries), 1)
        self.assertIn("8111 Broadway East", entries[0].title)
        self.assertIn("US 41", entries[0].title)

    def test_numbered_parenthetical_agenda_item_is_title_marker(self) -> None:
        text = (
            "(1) Development Order - Dunkin Donuts (DOS2022-E004) 10500 Corkscrew Road. "
            "Motion: Motion to approve. Action: Approved request with staff conditions. "
            "Vote: Aye: Board Members Jones Nay: Abstentions: "
            "(3) Land Development Code Amendment - Lot Coverage (Ordinance 2022-12). "
            "Motion: Motion to recommend approval of Ordinance 2022-12 to Council. "
            "Action: Recommended approval of Ordinance 2022-12 to Council. "
            "Vote: Aye: Board Members Jones Nay: Abstentions:"
        )

        entries = extract_agenda_entries(text)

        self.assertEqual(len(entries), 2)
        self.assertIn("Land Development Code Amendment", entries[1].title)
        self.assertNotIn("10500 Corkscrew Road", entries[1].title)

    def test_arcgis_suppresses_public_input_narrative_spillover(self) -> None:
        item = {
            "item_type": "No Action",
            "project_title": "approved by the ECCL members present at the August 28, 2015 meeting",
            "summary": (
                "approved by the ECCL members present at the August 28, 2015 meeting related to "
                "including civic assets near Koreshan State Site."
            ),
            "outcome": (
                "approved by the ECCL members present at the August 28, 2015 meeting related to "
                "including civic assets near Koreshan State Site. 10. COUNCIL COMMUNICATIONS AND "
                "FUTURE AGENDA ITEMS Councilmember Ribble reported that five offices have been "
                "located at The Brooks Executive Suites. 11. VILLAGE MANAGER COMMENTS Village "
                "Manager Peter Lombardi reported that all minutes would be abbreviated."
            ),
        }
        item_locations = [
            {"item_id": 23, "location_name": "Koreshan State Park"},
            {"item_id": 23, "location_name": "Estero River"},
            {"item_id": 23, "location_name": "The Brooks"},
        ]

        self.assertTrue(should_suppress_arcgis_item(item, item_locations))

    def test_arcgis_suppresses_single_location_approved_minutes_spillover(self) -> None:
        item = {
            "item_type": "Administrative",
            "project_title": "approved as presented: October 7, 2015 VILLAGE COUNCIL WORKSHOP",
            "summary": (
                "approved as presented: October 7, 2015 Village Council Workshop "
                "The Village Council Workshop was held at 21500 Three Oaks Parkway. "
                "1. CALL TO ORDER 2. ROLLCALL"
            ),
            "outcome": "approved as presented",
        }
        item_locations = [{"item_id": 25, "location_name": "21500 Three Oaks Parkway"}]

        self.assertTrue(should_suppress_arcgis_item(item, item_locations))

    def test_arcgis_suppresses_non_spatial_policy_testimony_location(self) -> None:
        item = {
            "item_type": "Ordinance",
            "project_title": "Ordinance regarding hydraulic fracturing and well stimulation",
            "summary": (
                "Council considered a general ordinance regarding hydraulic fracturing. "
                "A resident from Shadow Wood provided public testimony."
            ),
            "outcome": "Adopted the ordinance.",
        }
        item_locations = [{"item_id": 28, "location_name": "Shadow Wood"}]

        self.assertTrue(should_suppress_arcgis_item(item, item_locations))

    def test_location_matching_does_not_map_bert_harris_to_bert_trail(self) -> None:
        self.assertNotIn(
            "BERT Rail Trail Corridor",
            match_locations("Discussion followed regarding possible Bert Harris lawsuits."),
        )
        self.assertIn("BERT Rail Trail Corridor", match_locations("BERT Memorandum of Agreement"))

    def test_via_coconut_point_does_not_match_coconut_point_mall(self) -> None:
        # "Via Coconut Point" is a distinct street from the Coconut Point Mall.
        matches = match_locations("Via Coconut Point Landscape Design Contract")
        self.assertNotIn("Coconut Point", matches)
        self.assertIn("Via Coconut Point", matches)

    def test_coconut_point_mall_still_matches_directly(self) -> None:
        # Direct mall references should still bind to Coconut Point.
        matches = match_locations("Coconut Point Mall south of the movie theater")
        self.assertIn("Coconut Point", matches)

    def test_estero_parkway_reference_in_corridor_does_not_match(self) -> None:
        # "north of Estero Parkway" is a reference point, not a primary location.
        matches = match_locations("study area from US 41 south of Estero Parkway")
        self.assertNotIn("Estero Parkway", matches)

    def test_address_candidate_extracts_via_villagio(self) -> None:
        self.assertEqual(
            extract_address_candidates("23050 Via Villagio, Suite 101 Coconut Point Mall"),
            ["23050 Via Villagio"],
        )

    def test_address_candidate_extracts_strada_nuova(self) -> None:
        self.assertIn(
            "21450 Strada Nuova Circle",
            extract_address_candidates("Genova - 21450 Strada Nuova Circle Estero FL"),
        )

    def test_address_candidate_does_not_include_trailing_city(self) -> None:
        # The "Estero" trailing the address must not be captured into the Via pattern.
        candidates = extract_address_candidates("23050 Via Villagio Estero FL")
        for candidate in candidates:
            self.assertNotIn("Estero", candidate)
            self.assertNotIn("FL", candidate)

    def test_public_category_prefers_residential_over_procedure(self) -> None:
        text = (
            "Second Reading Ordinance No. 2025-06 Mayfair Village RPD Rezoning "
            "approving residential zoning amendments."
        )
        self.assertEqual(infer_category(text, "Ordinance"), "Residential Development")

    def test_public_category_detects_mobility_contracts(self) -> None:
        text = "Approved Contract EC 2024-07 to prepare the Village Wide Traffic Study."
        self.assertEqual(infer_category(text, "Contract Approval"), "Transportation & Mobility")

    def test_arcgis_keeps_site_specific_item_without_spillover(self) -> None:
        item = {
            "item_type": "Resolution",
            "project_title": "Resolution No. 2016-17 Supporting Collaborative Planning Efforts",
            "summary": "Resolution No. 2016-17 supporting collaborative planning efforts with Koreshan State Park.",
            "outcome": "Adopted Resolution No. 2016-17.",
        }
        item_locations = [{"item_id": 120, "location_name": "Koreshan State Park"}]

        self.assertFalse(should_suppress_arcgis_item(item, item_locations))

    # --- Category inference: Industry, Mining & Agriculture ---

    def test_category_industry_rock_mining(self) -> None:
        text = "Rock Mining Operation - Aggregate Extraction Permit (District 2)"
        self.assertEqual(infer_category(text, "Ordinance"), "Industry, Mining & Agriculture")

    def test_category_industry_agricultural_land_use(self) -> None:
        text = "Agricultural Land Use Rezoning - Rural Agricultural Designation Request"
        self.assertEqual(infer_category(text, "Resolution"), "Industry, Mining & Agriculture")

    def test_category_industry_warehouse_distribution(self) -> None:
        text = "Development Order for Warehouse and Distribution Center on Corkscrew Road"
        self.assertEqual(infer_category(text, "Ordinance"), "Industry, Mining & Agriculture")

    def test_category_industry_manufacturing_plant(self) -> None:
        text = "Site Plan Approval for Light Manufacturing Plant on Ben Hill Griffin Parkway"
        self.assertEqual(infer_category(text, "Resolution"), "Industry, Mining & Agriculture")

    # --- Category inference: Utilities, Stormwater & Environment ---

    def test_category_utilities_stormwater_drainage(self) -> None:
        text = "Contract for Stormwater Drainage Improvements on Williams Road"
        self.assertEqual(infer_category(text, "Contract Approval"), "Utilities, Stormwater & Environment")

    def test_category_utilities_wetland_mitigation(self) -> None:
        text = "Wetland Mitigation and Conservation Area Dedication - Estero River Preserve"
        self.assertEqual(infer_category(text, "Resolution"), "Utilities, Stormwater & Environment")

    def test_category_utilities_septic_to_sewer(self) -> None:
        text = "Task Authorization for Broadway Avenue East Septic to Sewer Utility Extension"
        self.assertEqual(infer_category(text, "Contract Approval"), "Utilities, Stormwater & Environment")

    def test_category_utilities_koreshan_environmental(self) -> None:
        text = "Environmental Monitoring Services Agreement for Koreshan State Park Shoreline"
        self.assertEqual(infer_category(text, "Contract Approval"), "Utilities, Stormwater & Environment")

    # --- Category inference: Public Facilities & Services ---

    def test_category_public_facilities_library(self) -> None:
        text = "South Regional Library Expansion and Parking Improvements"
        self.assertEqual(infer_category(text, "Resolution"), "Public Facilities & Services")

    def test_category_public_facilities_fire_rescue(self) -> None:
        text = "Agreement for Fire Rescue Station Design and Construction Services"
        self.assertEqual(infer_category(text, "Contract Approval"), "Public Facilities & Services")

    # --- Category inference: Budget, Contracts & Purchasing ---

    def test_category_budget_millage_adoption(self) -> None:
        text = "Ordinance No. 2024-10 Adopting the Millage Rate and Annual Budget for Fiscal Year 2025"
        self.assertEqual(infer_category(text, "Budget"), "Budget, Contracts & Purchasing")

    def test_category_budget_contract_fallback_no_subject(self) -> None:
        # Generic contract text with no subject-matter terms falls back via action_type
        text = "Approve Agreement with Firm for Village-Wide Professional Services"
        self.assertEqual(infer_category(text, "Contract Approval"), "Budget, Contracts & Purchasing")

    # --- Category inference: Meetings, Records & Public Input ---

    def test_category_meetings_approve_minutes(self) -> None:
        text = "Approval of Minutes - Regular Village Council Meeting of January 15, 2025"
        self.assertEqual(infer_category(text, "Administrative"), "Meetings, Records & Public Input")

    def test_category_meetings_cancelled_notice(self) -> None:
        text = "Village Council Regular Meeting Cancelled - No Quorum"
        self.assertEqual(infer_category(text, "No Action"), "Meetings, Records & Public Input")

    def test_category_ordinance_no_subject_falls_back_to_meetings(self) -> None:
        # Ordinance without subject-matter terms → non-spatial policy record
        text = "Ordinance No. 2024-01 Amending the Village Code"
        self.assertEqual(infer_category(text, "Ordinance"), "Meetings, Records & Public Input")

    def test_category_resolution_no_subject_falls_back_to_meetings(self) -> None:
        text = "Resolution No. 2024-05 Authorizing the Village Manager to Execute Documents"
        self.assertEqual(infer_category(text, "Resolution"), "Meetings, Records & Public Input")

    # --- Contract routing to underlying project category ---

    def test_category_transportation_contract_beats_budget_fallback(self) -> None:
        # Transportation-specific contract text → Transportation, not Budget
        text = "Contract for Ben Hill Griffin Parkway Roadway Improvements"
        self.assertEqual(infer_category(text, "Contract Approval"), "Transportation & Mobility")

    def test_category_utilities_contract_beats_budget_fallback(self) -> None:
        # Utility-specific contract text → Utilities, not Budget
        text = "Change Order for Estero River Dredging and Sediment Removal Services"
        self.assertEqual(infer_category(text, "Contract Approval"), "Utilities, Stormwater & Environment")

    def test_category_insurance_reimbursement_for_stormwater_routes_to_subject(self) -> None:
        # Even when support-category terms (insurance, reimbursement) outscore
        # subject terms numerically, the underlying subject must win.
        text = "Insurance reimbursement for stormwater pipe repair on Sandy Lane"
        self.assertEqual(infer_category(text, "Contract Approval"), "Utilities, Stormwater & Environment")

    def test_category_grant_for_fire_rescue_routes_to_subject(self) -> None:
        text = "Grant agreement accepting funding for fire rescue equipment"
        self.assertEqual(infer_category(text, "Contract Approval"), "Public Facilities & Services")

    # --- Cancellation detection ---

    def test_cancelled_meeting_inferred_from_filename(self) -> None:
        self.assertEqual(
            infer_meeting_type("2024-03-04 Cancelled Meeting Notice.pdf", "Routine body text"),
            "Cancelled Meeting",
        )

    def test_cancelled_meeting_inferred_from_notice_phrase(self) -> None:
        text = "NOTICE OF CANCELLATION\nThe Village Council meeting has been cancelled."
        self.assertEqual(
            infer_meeting_type("2024-03-04 Council Minutes.pdf", text),
            "Cancelled Meeting",
        )

    def test_regular_meeting_mentioning_cancellation_is_not_cancelled(self) -> None:
        # A regular meeting that merely references a separately cancelled item
        # in body text must not be classified as a Cancelled Meeting.
        text = (
            "Village Council Regular Meeting Minutes\n"
            "The Manager reported that the prior outreach event was cancelled due to weather. "
            "Council then approved the consent agenda."
        )
        self.assertEqual(
            infer_meeting_type("2024-03-04 Village Council Minutes.pdf", text),
            "Village Council Regular Meeting",
        )

    def test_action_type_no_action_only_for_meeting_cancellation(self) -> None:
        self.assertEqual(
            infer_action_type("The meeting has been cancelled due to lack of quorum.", "Village Council"),
            "No Action",
        )

    def test_action_type_does_not_flag_generic_cancelled_word(self) -> None:
        # An agenda action that simply discusses a cancelled contract / project
        # must not be downgraded to "No Action" by the meeting-cancellation rule.
        result = infer_action_type(
            "Approve resolution authorizing termination of the cancelled vendor contract.",
            "Village Council",
        )
        self.assertNotEqual(result, "No Action")

    # --- Meeting date extraction ---

    def test_parse_date_accepts_comma_and_commaless(self) -> None:
        self.assertEqual(parse_date("February 3, 2021"), "2021-02-03")
        self.assertEqual(parse_date("February 3 2021"), "2021-02-03")

    def test_extract_date_prefers_filename_over_boilerplate_text(self) -> None:
        # Some minutes open with boilerplate mentioning unrelated dates; the
        # filename date must win.
        text = (
            "Final Action Agenda/Minutes are supplemented by audio and video "
            "recordings. Video recordings of Village Council meetings from "
            "June 8, 2016 forward are available."
        )
        self.assertEqual(extract_date("03032021 minutes.pdf", text), ("2021-03-03", "filename"))

    def test_extract_date_falls_back_to_text_when_filename_garbled(self) -> None:
        # "0232021" (a typo for 02032021) has no parseable date; the header
        # date in the PDF text must be used instead of giving up.
        text = "Village Council Minutes - February 3, 2021 Page 1 of 6 APPROVED BY COUNCIL MARCH 3, 2021"
        self.assertEqual(extract_date("0232021 minutes.pdf", text), ("2021-02-03", "pdf_text"))

    def test_extract_date_text_skips_unparseable_word_dates(self) -> None:
        text = "Presented Page 3, 2015 and adopted on March 4, 2015 by the Council."
        self.assertEqual(extract_date("no-date.pdf", text), ("2015-03-04", "pdf_text"))

    # --- Agenda entry dedupe / action completeness ---

    def test_marker_and_section_extractions_merge_into_one_entry(self) -> None:
        # The Action: marker path and the section path both capture this item,
        # with action texts cut at different stop words. They must merge.
        text = (
            "BUSINESS ITEMS (a) Walmart Expansion (DOS2023-E008) (District 2) "
            "19975 S. Tamiami Trail. Staff Presentation/Comments Mary Gibbs. "
            "Action: Approved the Walmart expansion with conditions. "
            "Council Questions followed. "
            "Vote: Aye: Board Members Jones Nay: Abstentions: None "
            "BOARD COMMUNICATIONS Next meeting."
        )
        entries = extract_agenda_entries(text)
        walmart = [e for e in entries if "Walmart expansion" in e.action_text]
        self.assertEqual(len(walmart), 1)

    def test_passive_motion_phrasing_is_recognized_as_action(self) -> None:
        text = (
            "BUSINESS: (a) Consent Agenda Approval of January 14, 2025 meeting minutes. "
            "A motion to approve the consent agenda was made and duly passed. "
            "PUBLIC INPUT None."
        )
        entries = extract_agenda_entries(text)
        self.assertEqual(len(entries), 1)
        self.assertIn("duly passed", entries[0].action_text)
        self.assertNotIn("No formal action recorded", entries[0].action_text)
        self.assertNotIn("A motion", entries[0].title or "")

    def test_hyphenated_ordinance_number_is_not_truncated(self) -> None:
        text = (
            "Action: Adopted Ordinance No. 2025-16. "
            "Vote: (Roll Call) Aye: Councilmembers Hunt Nay: Abstentions:"
        )
        entries = extract_agenda_entries(text)
        self.assertEqual(entries[0].action_text, "Adopted Ordinance No. 2025-16")

    # --- Board routing / legacy stub suppression ---

    def test_pzdb_cancellation_routes_to_pzdb_board(self) -> None:
        builder = NormalizedBuilder(source_rows=[])
        self.assertEqual(
            builder._board_id_for("Cancelled Meeting", filename="01092024 PZDB cancellation.pdf"),
            2,
        )
        self.assertEqual(
            builder._board_id_for("Cancelled Meeting", filename="Cancel 020316 Council Meeting.pdf"),
            1,
        )

    def test_legacy_stub_suppressed_when_pdf_meeting_exists(self) -> None:
        builder = NormalizedBuilder(source_rows=[])
        builder.meetings.append({
            "meeting_id": 1,
            "board_id": 2,
            "meeting_date": "2024-01-09",
            "filename": "01092024 PZDB cancellation.pdf",
        })
        stub_row = {
            "MeetingType": "PZDB Meeting",
            "MeetingDate": "2024-01-09",
            "MinutesURL": "https://estero-fl.gov/01092024%20PZDB%20Minutes.pdf",
            "ActionTaken": "Approved the development order as presented",
        }
        builder.add_legacy_only_rows([stub_row])
        self.assertEqual(len(builder.meetings), 1)

        # Control: a different date must still create a legacy meeting.
        other_row = dict(stub_row, MeetingDate="2024-02-13",
                         MinutesURL="https://estero-fl.gov/02132024%20PZDB%20Minutes.pdf")
        builder.add_legacy_only_rows([other_row])
        self.assertEqual(len(builder.meetings), 2)


class MultiLocationResolverTests(unittest.TestCase):
    """Tests for resolve_all() fan-out behavior — multi-parcel detection
    without conflating descriptive context as separate sites.
    """

    def _build_stub_resolver(self, parcel_table: dict[tuple[str, str], list[dict]]):
        """Build a LocationResolver with a stubbed parcel client.

        parcel_table maps (street_number, street_substring_uppercase) → list
        of parcel hit dicts.  Street matching is substring against the
        canonicalized search variant uppercased.
        """
        from eaglegis.location_resolver import LocationResolver

        class StubRequester:
            def __init__(self):
                self.cache: dict = {}

            def flush(self):
                pass

        class StubParcelClient:
            def __init__(self, table):
                self.table = table

            def parcels_at_address(self, number, street_core):
                for (n, s), hits in self.table.items():
                    if n == number and s in street_core.upper():
                        return hits
                return []

            def parcel_at_point(self, lon, lat):
                return []

        resolver = LocationResolver.__new__(LocationResolver)
        resolver.requester = StubRequester()
        resolver.parcels = StubParcelClient(parcel_table)
        resolver.roads = None
        resolver.neighborhoods = None
        resolver.parks = None
        resolver.venue_lookup = {}
        resolver._venue_alias_index = []
        return resolver

    def test_multi_parcel_fans_out_into_separate_refs(self) -> None:
        # Via Coconut-style: 6 distinct addresses across 3 streets.  resolve_all
        # should return one ref per parcel, NOT a single averaged centroid.
        parcels = {
            ("8990", "CORKSCREW"): [
                {"STRAP": "S-CORK-8990", "SITEADDR": "8990 CORKSCREW RD",
                 "_lon": -81.789, "_lat": 26.435},
            ],
            ("21650", "VIA COCONUT"): [
                {"STRAP": "S-VC-21650", "SITEADDR": "21650 VIA COCONUT PT",
                 "_lon": -81.790, "_lat": 26.430},
            ],
            ("21750", "VIA COCONUT"): [
                {"STRAP": "S-VC-21750", "SITEADDR": "21750 VIA COCONUT PT",
                 "_lon": -81.791, "_lat": 26.431},
            ],
            ("21331", "HAPPY HOLLOW"): [
                {"STRAP": "S-HH-21331", "SITEADDR": "21331 HAPPY HOLLOW LN",
                 "_lon": -81.792, "_lat": 26.432},
            ],
            ("21350", "HAPPY HOLLOW"): [
                {"STRAP": "S-HH-21350", "SITEADDR": "21350 HAPPY HOLLOW LN",
                 "_lon": -81.793, "_lat": 26.433},
            ],
            ("21351", "HAPPY HOLLOW"): [
                {"STRAP": "S-HH-21351", "SITEADDR": "21351 HAPPY HOLLOW LN",
                 "_lon": -81.794, "_lat": 26.434},
            ],
        }
        resolver = self._build_stub_resolver(parcels)
        text = (
            "Via Coconut Development Order (D.O. # TBD) (District 5) "
            "8990 Corkscrew Road, 21650 & 21750 Via Coconut Point, "
            "21331, 21350 & 21351 Happy Hollow Lane. Properties are located "
            "south of Corkscrew Road and west of Via Coconut Point."
        )
        refs = resolver.resolve_all(text)
        straps = sorted(r.parcel_strap for r in refs if r.parcel_strap)
        self.assertEqual(
            straps,
            ["S-CORK-8990", "S-HH-21331", "S-HH-21350", "S-HH-21351",
             "S-VC-21650", "S-VC-21750"],
        )
        # First ref is primary; sequence stable.
        self.assertGreaterEqual(refs[0].confidence, 0.9)

    def test_directional_context_does_not_create_extra_site(self) -> None:
        # The "north of" phrase makes the second address descriptive context,
        # NOT a second site.  Only the primary address should be returned.
        parcels = {
            ("12345", "MAIN"): [
                {"STRAP": "PRIMARY", "SITEADDR": "12345 MAIN ST",
                 "_lon": -81.79, "_lat": 26.43},
            ],
            ("21500", "THREE OAKS"): [
                {"STRAP": "LANDMARK", "SITEADDR": "21500 THREE OAKS PKWY",
                 "_lon": -81.80, "_lat": 26.44},
            ],
        }
        resolver = self._build_stub_resolver(parcels)
        text = (
            "Project at 12345 Main Street, located 1,000 feet north of "
            "21500 Three Oaks Parkway."
        )
        refs = resolver.resolve_all(text)
        straps = sorted(r.parcel_strap for r in refs if r.parcel_strap)
        # Must NOT include the descriptive landmark.
        self.assertEqual(straps, ["PRIMARY"])

    def test_named_venue_in_directional_context_is_skipped(self) -> None:
        # The user-reported edge case: "north of the Estero Health Center"
        # is descriptive context, not a second site.  The primary address
        # is the only real location.
        parcels = {
            ("12345", "MAIN"): [
                {"STRAP": "PRIMARY", "SITEADDR": "12345 MAIN ST",
                 "_lon": -81.79, "_lat": 26.43},
            ],
        }
        resolver = self._build_stub_resolver(parcels)
        text = (
            "Project at 12345 Main Street, located just north of the "
            "Estero Health Center."
        )
        refs = resolver.resolve_all(text)
        # Only the primary parcel; no second ref pinned to the health center.
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].parcel_strap, "PRIMARY")

    def test_anchored_offset_does_not_become_extra_site(self) -> None:
        # "1,000 feet west of Corkscrew Road" is an anchored offset; the
        # mentioned street is the anchor, not a separate site.
        parcels = {
            ("10500", "CORKSCREW"): [
                {"STRAP": "PRIMARY", "SITEADDR": "10500 CORKSCREW RD",
                 "_lon": -81.79, "_lat": 26.43},
            ],
            ("8990", "CORKSCREW"): [
                {"STRAP": "ANCHOR-OTHER", "SITEADDR": "8990 CORKSCREW RD",
                 "_lon": -81.80, "_lat": 26.44},
            ],
        }
        resolver = self._build_stub_resolver(parcels)
        text = (
            "Estero Crossing 10500 Corkscrew Road, located on the south "
            "side of Corkscrew Road, 1,000 feet west of 8990 Corkscrew Road "
            "and I-75 intersection."
        )
        refs = resolver.resolve_all(text)
        straps = sorted(r.parcel_strap for r in refs if r.parcel_strap)
        # The 8990 mention is wrapped in "1,000 feet west of ..." — must be skipped.
        self.assertEqual(straps, ["PRIMARY"])

    def test_duplicate_address_dedup_by_parcel_strap(self) -> None:
        # If the same address is mentioned twice, resolve_all should not
        # create two refs.
        parcels = {
            ("10500", "CORKSCREW"): [
                {"STRAP": "ONLY", "SITEADDR": "10500 CORKSCREW RD",
                 "_lon": -81.79, "_lat": 26.43},
            ],
        }
        resolver = self._build_stub_resolver(parcels)
        text = (
            "Project at 10500 Corkscrew Road. The site at 10500 Corkscrew "
            "Road shall be developed in phases."
        )
        refs = resolver.resolve_all(text)
        self.assertEqual(len(refs), 1)

    def test_single_address_returns_single_ref(self) -> None:
        # The common case: one address, one ref.  No fan-out behavior should
        # alter the existing single-site contract.
        parcels = {
            ("10500", "CORKSCREW"): [
                {"STRAP": "ONLY", "SITEADDR": "10500 CORKSCREW RD",
                 "_lon": -81.79, "_lat": 26.43},
            ],
        }
        resolver = self._build_stub_resolver(parcels)
        refs = resolver.resolve_all("Estero Crossing Development at 10500 Corkscrew Road.")
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].location_type, "PARCEL_ADDRESS")
        self.assertEqual(refs[0].parcel_strap, "ONLY")

    def test_empty_text_returns_empty_list(self) -> None:
        resolver = self._build_stub_resolver({})
        self.assertEqual(resolver.resolve_all(""), [])
        self.assertEqual(resolver.resolve_all("   "), [])

    def test_ordinance_suffix_not_treated_as_second_house_number(self) -> None:
        # "Ordinance No. 2025-15, 4741 Broadway Avenue West" must yield only
        # the real parcel — the "-15" tail is a document number, not a second
        # house number on Broadway (which previously matched a parcel in
        # Fort Myers).
        parcels = {
            ("4741", "BROADWAY"): [
                {"STRAP": "GOOD", "SITEADDR": "4741 BROADWAY W",
                 "_lon": -81.833, "_lat": 26.441},
            ],
            ("15", "BROADWAY"): [
                {"STRAP": "BAD-FORT-MYERS", "SITEADDR": "15 BROADWAY CIR",
                 "_lon": -81.866, "_lat": 26.613},
            ],
        }
        resolver = self._build_stub_resolver(parcels)
        text = (
            "First Reading of Zoning Ordinance No. 2025-15, 4741 Broadway "
            "Avenue West rezoning An Ordinance of the Village Council. "
            "Action: Passed first reading of Ordinance No. 2025-15 and set "
            "second reading for January 21, 2026."
        )
        refs = resolver.resolve_all(text)
        straps = sorted(r.parcel_strap for r in refs if r.parcel_strap)
        self.assertEqual(straps, ["GOOD"])

    def test_road_fragment_not_pinned_to_distant_neighborhood(self) -> None:
        # "Three Oaks" is a parkway in Estero. It must not LIKE-match the
        # county neighborhoods layer (which contains "Three Oaks Marketplace"
        # in San Carlos Park, ~7 km north of the village).
        from eaglegis.location_resolver import LocationResolver

        class StubNeighborhoodClient:
            def __init__(self):
                self.queries: list[str] = []

            def neighborhoods_by_name(self, name_core):
                self.queries.append(name_core)
                if "THREE OAKS" in name_core:
                    return [{"name": "Three Oaks Marketplace Subdivision",
                             "_lon": -81.8037, "_lat": 26.4946}]
                return []

        class StubRoadClient:
            def segments_for_street(self, street_core):
                return []

            def segments_for_street_variants(self, variants):
                return []

        class StubParkClient:
            def parks_by_name(self, name_core):
                return []

        class StubParcelClient:
            def parcels_at_address(self, number, street_core):
                return []

            def parcel_at_point(self, lon, lat):
                return []

        resolver = LocationResolver.__new__(LocationResolver)
        resolver.requester = None
        resolver.parcels = StubParcelClient()
        resolver.roads = StubRoadClient()
        resolver.neighborhoods = StubNeighborhoodClient()
        resolver.parks = StubParkClient()
        resolver.venue_lookup = {}
        resolver._venue_alias_index = []

        text = (
            "US 41 & Three Oaks Monument Sign Construction Plans & Permitting "
            "Contract. Action: Approved award of Supplemental Task "
            "Authorization (STA) - 01."
        )
        refs = resolver.resolve_all(text)
        self.assertEqual(refs, [])
        self.assertNotIn("THREE OAKS", " ".join(resolver.neighborhoods.queries))


if __name__ == "__main__":
    unittest.main()

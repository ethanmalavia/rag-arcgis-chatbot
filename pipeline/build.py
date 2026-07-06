from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlparse

from eaglegis.classifiers import (
    extract_address_candidates,
    infer_action_type,
    infer_category,
    match_locations,
    match_projects,
    needs_review,
    vote_detected,
)
from eaglegis.config import (
    CATEGORY_DEFINITIONS,
    LOCATION_SEEDS,
    PROJECT_ALIASES,
    SITE_LOCATION_OVERRIDES,
    SITE_TEXT_LOCATION_OVERRIDES,
)
from eaglegis.location_resolver import LocationReference, LocationResolver
from eaglegis.extractors import (
    extract_agenda_entries,
    extract_date,
    extract_end_time,
    extract_staff_code,
    extract_start_time,
    infer_meeting_type,
    normalize_meeting_type,
    raw_pdf_url,
    split_csv_actions,
)
from eaglegis.gold import AI_PUBLIC_FIELDS, build_ai_public_rows
from eaglegis.sources import PdfAsset, iter_git_pdfs, iter_local_pdfs, read_git_text
from eaglegis.text import extract_pdf_text
from eaglegis.writer import write_csv


ACTION_TYPE_VALUES = {
    "Vote", "Motion", "Discussion", "Presentation", "Public Comment",
    "Public Hearing", "Consent Agenda", "Ordinance", "Resolution",
    "Contract Approval", "Budget", "Administrative", "No Action", "Unknown",
}

# Cancellation notices carry meeting_type "Cancelled Meeting", which hides the
# board — recover it from the filename ("01092024 PZDB cancellation.pdf").
PZDB_FILENAME_RE = re.compile(r"pzdb|planning[\s,]*zoning", re.I)

BOARD_SEEDS = [
    {"board_id": 1, "code": "VC", "name": "Village Council", "active_from": "2014-12-31", "active_to": None},
    {"board_id": 2, "code": "PZDB", "name": "Planning Zoning & Design Board", "active_from": None, "active_to": None},
]

FORMAT_SEEDS = [
    {"format_id": 1, "name": "Regular Meeting", "description": "Standard board meeting"},
    {"format_id": 2, "name": "Special Meeting", "description": "Specially called meeting"},
    {"format_id": 3, "name": "Workshop", "description": "Workshop or discussion meeting"},
    {"format_id": 4, "name": "Joint Workshop", "description": "Joint workshop meeting"},
    {"format_id": 5, "name": "Zoning Hearing", "description": "Zoning or development order hearing"},
    {"format_id": 6, "name": "Organizational Meeting", "description": "Organizational business meeting"},
    {"format_id": 7, "name": "Emergency Meeting", "description": "Special emergency meeting"},
    {"format_id": 8, "name": "Budget Hearing", "description": "Budget or millage hearing"},
    {"format_id": 9, "name": "Combined Hearing / Workshop", "description": "Combined hearing and workshop"},
    {"format_id": 10, "name": "Comprehensive Plan Workshop", "description": "Comprehensive plan workshop"},
    {"format_id": 11, "name": "Cancelled", "description": "Cancelled meeting notice"},
    {"format_id": 12, "name": "Public Information Meeting", "description": "Public information meeting or open house"},
    {"format_id": 13, "name": "Public Hearing", "description": "Public hearing"},
]

MEETING_TYPE_SEEDS = [
    {"type_id": 1, "type_name": "Village Council", "description": "Regular Village Council meeting"},
    {"type_id": 2, "type_name": "Planning Zoning & Design Board", "description": "Combined planning, zoning, and design review board meeting"},
    {"type_id": 3, "type_name": "Public Hearing", "description": "Public input sessions on proposed projects"},
    {"type_id": 4, "type_name": "Workshop", "description": "Informational workshops for Council and boards"},
]


def _load_estero_url_lookup(data_dir: Path) -> dict[str, str]:
    lookup_file = data_dir / "bronze" / "estero_minutes_urls.txt"
    if not lookup_file.exists():
        return {}
    lookup: dict[str, str] = {}
    with lookup_file.open(encoding="utf-8") as f:
        for line in f:
            url = line.strip()
            if url:
                key = re.sub(r"\s+", " ", unquote(url.split("/")[-1]).lower().strip())
                lookup[key] = url
    return lookup


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)

    source_rows = load_source_rows(args)
    rows_by_filename = index_source_rows(source_rows)

    assets = load_pdf_assets(args)
    if not assets:
        raise SystemExit("No PDFs found. Provide --pdf-dir or --git-ref.")

    estero_url_lookup = _load_estero_url_lookup(Path(__file__).parent.parent / "backend" / "data")

    builder = NormalizedBuilder(source_rows=source_rows)
    asset_filenames = {asset.filename.lower() for asset in assets}
    for asset in assets:
        filename_key = re.sub(r"\s+", " ", asset.filename.lower().strip())
        estero_url = estero_url_lookup.get(filename_key)
        builder.add_pdf(asset, rows_by_filename.get(asset.filename.lower(), []), estero_url=estero_url)
    builder.add_legacy_only_rows([
        row for row in source_rows
        if (filename_from_url(row.get("MinutesURL") or "") or "").lower() not in asset_filenames
    ])

    builder.write(out_dir)
    builder._location_resolver.flush()
    print(f"Wrote normalized CSVs to {out_dir}")
    print(f"Meetings: {len(builder.meetings)}")
    print(f"Documents: {len(builder.documents)}")
    print(f"Agenda items: {len(builder.agenda_items)}")
    print(f"Review rows: {len(builder.review_rows)}")
    print(
        f"Resolver: calls={builder._location_resolver.requester.calls}, "
        f"cache_hits={builder._location_resolver.requester.hits}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build normalized EagleGIS CSVs from meeting PDFs and optional legacy CSV data."
    )
    parser.add_argument("--pdf-dir", default="pdfs", help="Local PDF directory.")
    parser.add_argument(
        "--git-ref",
        default=None,
        help="Read PDFs from a git ref, e.g. origin/script. Used automatically if pdf-dir is missing.",
    )
    parser.add_argument(
        "--source-csv",
        default=None,
        help="Optional legacy Estero_Meetings_Final.csv path.",
    )
    parser.add_argument(
        "--source-git-ref",
        default="origin/script",
        help="Git ref used to read pdfs/Estero_Meetings_Final.csv if --source-csv is absent.",
    )
    parser.add_argument(
        "--source-git-path",
        default="pdfs/Estero_Meetings_Final.csv",
        help="Path to legacy CSV inside --source-git-ref.",
    )
    parser.add_argument("--out-dir", default="backend/data", help="Output directory.")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional page limit for fast debugging; default reads the full PDF.",
    )
    return parser.parse_args()


def load_pdf_assets(args: argparse.Namespace) -> list[PdfAsset]:
    pdf_dir = Path(args.pdf_dir)
    if args.git_ref:
        return iter_git_pdfs(args.git_ref)
    if pdf_dir.exists():
        return iter_local_pdfs(pdf_dir)
    return iter_git_pdfs("origin/script")


def load_source_rows(args: argparse.Namespace) -> list[dict]:
    if args.source_csv and Path(args.source_csv).exists():
        text = Path(args.source_csv).read_text(encoding="utf-8-sig")
    else:
        text = read_git_text(args.source_git_ref, args.source_git_path)
    if not text:
        return []
    return list(csv.DictReader(text.splitlines()))


def index_source_rows(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        filename = filename_from_url(row.get("MinutesURL") or row.get("Document_Link") or "")
        if filename:
            out[filename.lower()].append(row)
    return out


def filename_from_url(url: str) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    path = unquote(parsed.path)
    name = Path(path).name
    return name or None


def title_for(meeting_type: str, meeting_date: str | None) -> str:
    date_part = meeting_date or "Unknown Date"
    if meeting_type == "Cancelled Meeting":
        return f"Cancelled Meeting Notice - {date_part}"
    if "Planning Zoning" in meeting_type:
        return f"PZ&DB Meeting Minutes - {date_part}"
    if meeting_type == "Joint Workshop":
        return f"Joint Workshop Minutes - {date_part}"
    if meeting_type == "Comprehensive Plan Workshop":
        return f"Comprehensive Plan Workshop Minutes - {date_part}"
    if meeting_type == "Combined Zoning Hearing / Workshop":
        return f"Zoning Hearing and Workshop Minutes - {date_part}"
    if meeting_type == "Workshop":
        return f"Village Council Workshop Minutes - {date_part}"
    if meeting_type == "Special Emergency Meeting":
        return f"Special Emergency Meeting Minutes - {date_part}"
    if meeting_type == "Special Meeting":
        return f"Special Meeting Minutes - {date_part}"
    if meeting_type == "Organizational Meeting":
        return f"Organizational Meeting Minutes - {date_part}"
    if "Hearing" in meeting_type:
        return f"{meeting_type} Minutes - {date_part}"
    if "Public Information" in meeting_type:
        return f"Public Information Meeting Minutes - {date_part}"
    return f"Village Council Meeting Minutes - {date_part}"


def infer_venue(text: str, rows: list[dict]) -> tuple[str, str | None]:
    lo = text.lower()
    if "legacy church" in lo:
        return "Legacy Church", "pdf_text"
    if "estero fire rescue" in lo:
        return "Estero Fire Rescue", "pdf_text"
    if "three oaks parkway" in lo:
        return "Estero Fire Rescue", "pdf_text"
    if "corkscrew palms" in lo or "council chambers" in lo:
        return "Estero Village Hall", "pdf_text"
    if rows:
        loc = rows[0].get("LocationName")
        if loc and loc == "Estero Village Hall":
            return loc, "legacy_csv"
    return "Estero Village Hall", "default"


def distinct(values: list[str | None]) -> list[str]:
    out = []
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


class NormalizedBuilder:
    def __init__(self, source_rows: list[dict]) -> None:
        self.source_rows = source_rows
        self.boards: list[dict] = [dict(row) for row in BOARD_SEEDS]
        self.meeting_formats: list[dict] = [dict(row) for row in FORMAT_SEEDS]
        self.meeting_types: list[dict] = [dict(row) for row in MEETING_TYPE_SEEDS]
        self.meetings: list[dict] = []
        self.documents: list[dict] = []
        self.documents_v2: list[dict] = []
        self.projects: list[dict] = []
        self.locations: list[dict] = []
        self.locations_v2: list[dict] = []
        self.arcgis_rows: list[dict] = []
        self.agenda_categories: list[dict] = []
        self.agenda_items: list[dict] = []
        self.agenda_item_projects: list[dict] = []
        self.agenda_item_locations: list[dict] = []
        self.motions: list[dict] = []
        self.location_candidates: list[dict] = []
        self.unmapped_agenda_items: list[dict] = []
        self.review_rows: list[dict] = []

        self._board_ids = {row["name"]: int(row["board_id"]) for row in self.boards}
        self._format_ids = {row["name"]: int(row["format_id"]) for row in self.meeting_formats}
        self._meeting_type_ids: dict[str, int] = {
            row["type_name"]: int(row["type_id"]) for row in self.meeting_types
        }
        self._project_ids: dict[str, int] = {}
        self._location_ids: dict[str, int] = {}
        self._category_ids: dict[str, int] = {}
        self._meeting_keys: dict[tuple[str, str, str], int] = {}
        self._legacy_location_lookup = self._build_legacy_location_lookup(source_rows)

        self._seed_categories()
        self._seed_projects(source_rows)
        self._seed_locations(source_rows)

        # Typed location resolver: produces one (lat, lon) per agenda item,
        # tagged with the resolution strategy (parcel, corridor, intersection,
        # whole-street, named-venue, neighborhood, anchored-offset).
        self._location_resolver = LocationResolver(venue_lookup=LOCATION_SEEDS)

    def _build_legacy_location_lookup(self, source_rows: list[dict]) -> dict[str, dict]:
        lookup: dict[str, dict] = {}
        for row in source_rows:
            name = row.get("LocationName")
            lat = row.get("Latitude")
            lon = row.get("Longitude")
            if not name or not lat or not lon:
                continue
            lookup[name.lower()] = {
                "location_name": name,
                "address": row.get("LocationName") or "",
                "latitude": lat,
                "longitude": lon,
                "project_name": row.get("ProjectName"),
            }
        return lookup

    def _seed_projects(self, source_rows: list[dict]) -> None:
        for project in PROJECT_ALIASES:
            self._project_id(project)
        for project in distinct([r.get("ProjectName") for r in source_rows]):
            self._project_id(project)

    def _seed_categories(self) -> None:
        for definition in CATEGORY_DEFINITIONS:
            self._category_id(str(definition["name"]), str(definition.get("description") or ""))

    def _seed_locations(self, source_rows: list[dict]) -> None:
        for name, data in LOCATION_SEEDS.items():
            self._location_id(
                name,
                location_type=str(data["location_type"]),
                address=str(data.get("address") or ""),
                latitude=data.get("latitude"),
                longitude=data.get("longitude"),
            )
        for row in source_rows:
            name = row.get("LocationName")
            if not name:
                continue
            location_id = self._location_id(
                name,
                location_type="General Area",
                address="",
                latitude=row.get("Latitude"),
                longitude=row.get("Longitude"),
            )
            self._fill_location_coordinates(
                location_id,
                latitude=row.get("Latitude"),
                longitude=row.get("Longitude"),
            )

    def _fill_location_coordinates(self, location_id: int, *, latitude: object, longitude: object) -> None:
        if latitude in (None, "") or longitude in (None, ""):
            return
        loc = self._location_by_id(location_id)
        if not loc:
            return
        if loc.get("latitude") in (None, "") or loc.get("longitude") in (None, ""):
            loc["latitude"] = latitude
            loc["longitude"] = longitude

    def add_pdf(self, asset: PdfAsset, legacy_rows: list[dict], estero_url: str | None = None) -> None:
        text, page_count, needs_ocr = extract_pdf_text(asset.data)
        fallback = legacy_rows[0] if legacy_rows else {}

        date, date_source = extract_date(asset.filename, text)
        if not date and fallback.get("MeetingDate"):
            date = fallback["MeetingDate"]
            date_source = "legacy_csv"

        meeting_type = infer_meeting_type(
            asset.filename,
            text,
            fallback.get("MeetingType") or fallback.get("Meeting Type"),
        )
        grouped_meeting_type = grouped_meeting_type_for(meeting_type, filename=asset.filename)
        type_id = self._meeting_type_id(grouped_meeting_type)
        board_id = self._board_id_for(meeting_type, filename=asset.filename)
        format_id = self._format_id_for(meeting_type)
        venue_name, venue_source = infer_venue(text, legacy_rows)
        start_time = extract_start_time(text) or fallback.get("StartTime") or fallback.get("Start Time")
        end_time = extract_end_time(text) or fallback.get("End Time")
        staff_code = extract_staff_code(text) or fallback.get("StaffCode") or fallback.get("Staff Code")
        status = "Cancelled" if meeting_type == "Cancelled Meeting" else (fallback.get("Status") or "Accepted")
        preferred_pdf_url = fallback.get("MinutesURL") or estero_url or raw_pdf_url(asset.filename)

        meeting_key = (meeting_type, date or "unknown", asset.filename.lower())
        meeting_id = self._meeting_keys.get(meeting_key)
        if meeting_id is None:
            meeting_id = len(self.meetings) + 1
            self._meeting_keys[meeting_key] = meeting_id
            self.meetings.append({
                "meeting_id": meeting_id,
                "board_id": board_id,
                "format_id": format_id,
                "legacy_meeting_id": meeting_id,
                "title": title_for(meeting_type, date),
                "meeting_time": start_time,
                "meeting_location": venue_name,
                "pdf_url": preferred_pdf_url,
                "raw_text": text,
                "summary": grouped_meeting_type,
                "filename": asset.filename,
                "type_id": type_id,
                "meeting_date": date,
                "meeting_year": date[:4] if date else None,
                "venue_location_id": self._venue_location_id(venue_name),
                "venue_name": venue_name,
                "venue_address": self._venue_address(venue_name),
                "start_time": start_time,
                "end_time": end_time,
                "status": status,
                "notes": f"date_source={date_source}; venue_source={venue_source}; pages={page_count}; detailed_type={meeting_type}",
            })

        self.documents.append({
            "document_id": len(self.documents) + 1,
            "meeting_id": meeting_id,
            "title": title_for(meeting_type, date),
            "document_type": "Minutes",
            "file_name": asset.filename,
            "file_url": preferred_pdf_url,
            "upload_date": None,
            "doc_date": fallback.get("DocDate") or date,
            "notes": None,
        })
        self.documents_v2.append({
            "document_id": len(self.documents_v2) + 1,
            "meeting_id": meeting_id,
            "legacy_document_id": len(self.documents),
            "document_type": "Minutes",
            "title": title_for(meeting_type, date),
            "file_url": preferred_pdf_url,
            "uploaded_at": None,
        })

        agenda_entries = extract_agenda_entries(text)
        actions = [entry.action_text for entry in agenda_entries]
        used_csv_fallback = False
        if not actions:
            for row in legacy_rows:
                actions.extend(split_csv_actions(row.get("ActionTaken") or row.get("Action Taken")))
            actions = distinct(actions)
            agenda_entries = []
            used_csv_fallback = bool(actions)
        if status == "Cancelled" and not actions:
            actions = ["Meeting Cancelled"]
            agenda_entries = []

        fallback_projects = distinct([r.get("ProjectName") for r in legacy_rows])
        fallback_locations = distinct([r.get("LocationName") for r in legacy_rows])

        if not actions:
            self._add_review(asset, meeting_id, needs_ocr, "No agenda/action items extracted.")
            return

        for order, action in enumerate(actions, start=1):
            entry = agenda_entries[order - 1] if order <= len(agenda_entries) else None
            entry_title = entry.title if entry else None
            self._add_action(
                meeting_id=meeting_id,
                item_order=order,
                meeting_type=meeting_type,
                item_title=entry_title,
                action_text=action,
                vote_text=entry.vote_text if entry else None,
                staff_code=staff_code,
                needs_ocr=needs_ocr,
                date_missing=date is None,
                used_csv_fallback=used_csv_fallback,
                fallback_projects=fallback_projects,
                fallback_locations=fallback_locations,
                asset=asset,
            )

    def add_legacy_only_rows(self, rows: list[dict]) -> None:
        grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for row in rows:
            url = row.get("MinutesURL") or row.get("Document_Link") or ""
            meeting_type = normalize_meeting_type(row.get("MeetingType") or row.get("Meeting Type")) or "Unknown"
            meeting_date = row.get("MeetingDate") or row.get("ArcGIS_Date") or row.get("DocDate") or ""
            grouped[(meeting_type, meeting_date, url)].append(row)

        # Meetings already built from a local PDF, keyed by (board, date).
        # A legacy row for the same board and date is a stale duplicate: the
        # CSV references e.g. "01092024 PZDB Minutes.pdf" while the actual
        # document is "01092024 PZDB cancellation.pdf" — without this check
        # the cancelled meeting would also appear as a phantom "Accepted"
        # meeting with no items.
        pdf_meeting_keys = {
            (m["board_id"], m["meeting_date"])
            for m in self.meetings
            if m.get("meeting_date")
        }

        for (_, _, url), group in grouped.items():
            row = group[0]
            meeting_type = normalize_meeting_type(row.get("MeetingType") or row.get("Meeting Type")) or "Unknown"
            filename = filename_from_url(url) or row.get("Title") or f"legacy-{len(self.documents) + 1}.pdf"
            grouped_meeting_type = grouped_meeting_type_for(meeting_type, filename=filename)
            meeting_date = row.get("MeetingDate") or row.get("ArcGIS_Date") or row.get("DocDate") or None
            type_id = self._meeting_type_id(grouped_meeting_type)
            board_id = self._board_id_for(meeting_type, filename=filename)
            if meeting_date and (board_id, meeting_date) in pdf_meeting_keys:
                continue
            format_id = self._format_id_for(meeting_type)
            venue_name = row.get("LocationName") if row.get("LocationName") == "Estero Village Hall" else "Estero Village Hall"
            meeting_key = (meeting_type, meeting_date or "unknown", url)

            meeting_id = self._meeting_keys.get(meeting_key)
            if meeting_id is None:
                meeting_id = len(self.meetings) + 1
                self._meeting_keys[meeting_key] = meeting_id
                self.meetings.append({
                    "meeting_id": meeting_id,
                    "board_id": board_id,
                    "format_id": format_id,
                    "legacy_meeting_id": meeting_id,
                    "title": title_for(meeting_type, meeting_date),
                    "meeting_time": row.get("StartTime") or row.get("Start Time"),
                    "meeting_location": venue_name,
                    "pdf_url": url,
                    "raw_text": None,
                    "summary": grouped_meeting_type,
                    "filename": filename,
                    "type_id": type_id,
                    "meeting_date": meeting_date,
                    "meeting_year": row.get("MeetingYear") or (meeting_date[:4] if meeting_date else None),
                    "venue_location_id": self._venue_location_id(venue_name),
                    "venue_name": venue_name,
                    "venue_address": self._venue_address(venue_name),
                    "start_time": row.get("StartTime") or row.get("Start Time"),
                    "end_time": None,
                    "status": row.get("Status") or "Pending",
                    "notes": f"legacy_csv_only; pdf_not_available_locally; detailed_type={meeting_type}",
                })

            self.documents.append({
                "document_id": len(self.documents) + 1,
                "meeting_id": meeting_id,
                "title": title_for(meeting_type, meeting_date),
                "document_type": "Minutes",
                "file_name": filename,
                "file_url": url,
                "upload_date": None,
                "doc_date": row.get("DocDate") or meeting_date,
                "notes": "legacy_csv_only",
            })
            self.documents_v2.append({
                "document_id": len(self.documents_v2) + 1,
                "meeting_id": meeting_id,
                "legacy_document_id": len(self.documents),
                "document_type": "Minutes",
                "title": title_for(meeting_type, meeting_date),
                "file_url": url,
                "uploaded_at": None,
            })

            actions: list[str] = []
            for legacy in group:
                actions.extend(split_csv_actions(legacy.get("ActionTaken") or legacy.get("Action Taken")))
            actions = distinct(actions)
            if not actions:
                self._add_review_filename(filename, meeting_id, False, "Legacy-only row has no extractable action text.")
                continue
            for order, action in enumerate(actions, start=1):
                self._add_action(
                meeting_id=meeting_id,
                item_order=order,
                meeting_type=meeting_type,
                item_title=None,
                    action_text=action,
                    vote_text=None,
                    staff_code=row.get("StaffCode") or row.get("Staff Code"),
                    needs_ocr=False,
                    date_missing=meeting_date is None,
                    used_csv_fallback=True,
                    fallback_projects=[],
                    fallback_locations=[],
                    asset=PdfAsset(path=url, filename=filename, data=b""),
                )

    def _add_action(
        self,
        *,
        meeting_id: int,
        item_order: int,
        meeting_type: str,
        item_title: str | None,
        action_text: str,
        vote_text: str | None,
        staff_code: str | None,
        needs_ocr: bool,
        date_missing: bool,
        used_csv_fallback: bool,
        fallback_projects: list[str],
        fallback_locations: list[str],
        asset: PdfAsset,
    ) -> None:
        action_type = infer_action_type(action_text, meeting_type)
        if action_type not in ACTION_TYPE_VALUES:
            action_type = "Unknown"

        # Do not inherit the legacy row's project/location blindly. The old CSV
        # often assigns one whole PDF to one bucket even when its agenda items
        # cover unrelated actions. Only link a project/place when the agenda
        # item text itself contains matching evidence.
        item_text = action_text if not item_title else f"{item_title}. Action: {action_text}"
        category_id = self._category_id(infer_category(item_text, action_type))
        title_context = item_title or action_text
        project_names = match_projects(title_context)
        location_names = match_locations(title_context)
        review = needs_review(
            needs_ocr=needs_ocr,
            date_missing=date_missing,
            action_count=1,
            project_count=len(project_names),
            location_count=len(location_names),
            used_csv_fallback=used_csv_fallback,
        )

        item_id = len(self.agenda_items) + 1
        display_title = item_title or action_text[:90]
        if self._has_duplicate_agenda_item(meeting_id, item_text, action_text):
            return
        motion_text = infer_motion_text(action_text)
        vote_context = f"Vote: {vote_text}" if vote_text else action_text
        application_id = infer_application_id(item_text)
        override_location = site_location_override(application_id, item_text)
        if override_location:
            location_names = []
        self.agenda_items.append({
            "item_id": item_id,
            "meeting_id": meeting_id,
            "item_number": str(item_order),
            "item_type": action_type,
            "application_id": application_id,
            "applicant_name": infer_applicant_name(item_text),
            "project_title": display_title[:500],
            "district": infer_district(item_text),
            "address_raw": str(override_location.get("address")) if override_location else first_address_or_location(item_text, location_names),
            "summary": item_text,
            "outcome": action_text,
            "motion_text": motion_text,
            "vote_result": infer_vote_result(vote_context),
            "created_at": None,
            "project_matches": "; ".join(project_names),
            "staff_code": staff_code,
            "needs_review": review,
            "extraction_confidence": confidence_score(review, needs_ocr, used_csv_fallback, project_names, location_names),
            "category_id": category_id,
            "item_order": item_order,
            "item_title": display_title[:500],
            "item_text": item_text,
            "action_taken": action_text,
            "action_type": action_type,
            "vote_detected": vote_detected(action_text),
            "extraction_notes": "csv_fallback" if used_csv_fallback else None,
        })
        if motion_text or vote_detected(action_text):
            vote_yes, vote_no, vote_abstain = infer_vote_counts(vote_context)
            self.motions.append({
                "motion_id": len(self.motions) + 1,
                "item_id": item_id,
                "motion_text": motion_text or action_text,
                "proposed_by": infer_motion_person(item_text, "Motion by"),
                "seconded_by": infer_motion_person(item_text, "Seconded by"),
                "outcome": action_text,
                "vote_yes": vote_yes,
                "vote_no": vote_no,
                "vote_abstain": vote_abstain,
                "created_at": None,
            })

        address_candidates = [
            normalize_address_candidate(a)
            for a in extract_address_candidates(item_text)
        ]
        address_candidates = filter_address_candidates(
            address_candidates,
            text=item_text,
            action_type=action_type,
            application_id=application_id,
            project_names=project_names,
        )
        if override_location:
            address_candidates = []

        # Typed location resolver: returns ONE point per distinct site the
        # agenda item references.  Most items have one site; multi-parcel
        # items (e.g. "8990 Corkscrew Road, 21650 & 21750 Via Coconut Point")
        # fan out into one ref per parcel.  Descriptive context like
        # "north of X" is NOT treated as a separate site.
        resolved_refs: list[LocationReference] = []
        if not override_location:
            try:
                resolved_refs = self._location_resolver.resolve_all(item_text, item_title=item_title)
            except RuntimeError:
                resolved_refs = []

        for project in project_names:
            self.agenda_item_projects.append({
                "item_id": item_id,
                "project_id": self._project_id(project),
            })

        # Authoritative location: resolver result wins over address-regex and
        # seed fallbacks. We still write a seed/address row for items the
        # resolver couldn't classify, so the map keeps its existing coverage.
        if resolved_refs:
            for seq, ref in enumerate(resolved_refs, start=1):
                display_name = ref.address_label or ref.raw_text or item_title or "Resolved Location"
                location_id = self._location_id(
                    display_name,
                    location_type=ref.location_type,
                    address=ref.address_label or display_name,
                    latitude=ref.latitude,
                    longitude=ref.longitude,
                )
                self.agenda_item_locations.append({
                    "item_id": item_id,
                    "location_id": location_id,
                })
                self._add_location_v2(
                    item_id=item_id,
                    address_raw=ref.raw_text or display_name,
                    address_normalized=ref.address_label or display_name,
                    latitude=ref.latitude,
                    longitude=ref.longitude,
                    geocode_confidence=ref.confidence,
                    location_name=display_name,
                    project_name=project_names[0] if project_names else None,
                    location_type=ref.location_type,
                    resolution_notes=ref.resolution_notes,
                    location_seq=seq,
                    is_primary=(seq == 1),
                    parcel_id=ref.parcel_strap or None,
                )
        else:
            seed_locations_to_write = [] if address_candidates else location_names
            for location in seed_locations_to_write:
                location_id = self._location_id(location)
                self.agenda_item_locations.append({
                    "item_id": item_id,
                    "location_id": location_id,
                })
                self._add_location_v2_for_seed(item_id, location_id)
                self._add_location_candidate_for_seed(item_id, meeting_id, location_id, location, action_text)

            for address in address_candidates:
                location_id = self._location_id(
                    address,
                    location_type="General Area",
                    address=ensure_estero_address(address),
                )
                self.agenda_item_locations.append({
                    "item_id": item_id,
                    "location_id": location_id,
                })
                self._add_location_v2(
                    item_id=item_id,
                    address_raw=address,
                    address_normalized=ensure_estero_address(address),
                    latitude=None,
                    longitude=None,
                    geocode_confidence=0.25,
                    location_name=address,
                    project_name=project_names[0] if project_names else None,
                )
                self._add_location_candidate(
                    item_id=item_id,
                    meeting_id=meeting_id,
                    location_id=location_id,
                    candidate_name=address,
                    candidate_address=ensure_estero_address(address),
                    candidate_source="address_regex",
                    evidence=action_text,
                )
        if override_location:
            address = str(override_location["address"])
            location_id = self._location_id(
                address,
                location_type="Project Site",
                address=address,
                latitude=override_location.get("latitude"),
                longitude=override_location.get("longitude"),
            )
            self.agenda_item_locations.append({
                "item_id": item_id,
                "location_id": location_id,
            })
            self._add_location_v2(
                item_id=item_id,
                address_raw=address,
                address_normalized=address,
                latitude=override_location.get("latitude"),
                longitude=override_location.get("longitude"),
                geocode_confidence=float(override_location.get("confidence") or 0.9),
                location_name=address,
                project_name=project_names[0] if project_names else None,
            )
        if not location_names and not address_candidates and not override_location:
            self.unmapped_agenda_items.append({
                "item_id": item_id,
                "meeting_id": meeting_id,
                "action_type": action_type,
                "vote_detected": vote_detected(action_text),
                "project_matches": "; ".join(project_names),
                "review_reason": "No location name/address matched",
                "item_text": action_text,
            })
        if review:
            self._add_review(asset, meeting_id, needs_ocr, f"Review item {item_id}: weak or fallback extraction.")

    def _has_duplicate_agenda_item(self, meeting_id: int, item_text: str, action_text: str) -> bool:
        item_key = re.sub(r"\s+", " ", item_text).strip().lower()
        action_key = re.sub(r"\s+", " ", action_text).strip().lower()
        for row in self.agenda_items:
            if row.get("meeting_id") != meeting_id:
                continue
            existing_item = re.sub(r"\s+", " ", str(row.get("summary") or "")).strip().lower()
            existing_action = re.sub(r"\s+", " ", str(row.get("outcome") or "")).strip().lower()
            if existing_item == item_key and existing_action == action_key:
                return True
        return False

    def _meeting_type_id(self, name: str) -> int:
        grouped_name = grouped_meeting_type_for(name)
        return self._meeting_type_ids[grouped_name]

    def _board_id_for(self, meeting_type: str, filename: str = "") -> int:
        is_pzdb = "Planning Zoning" in meeting_type or bool(PZDB_FILENAME_RE.search(filename or ""))
        name = "Planning Zoning & Design Board" if is_pzdb else "Village Council"
        if name not in self._board_ids:
            board_id = len(self.boards) + 1
            self._board_ids[name] = board_id
            code = re.sub(r"[^A-Z0-9]+", "", "".join(part[0] for part in name.upper().split()))
            self.boards.append({
                "board_id": board_id,
                "code": code or f"BOARD{board_id}",
                "name": name,
                "active_from": None,
                "active_to": None,
            })
        return self._board_ids[name]

    def _format_id_for(self, meeting_type: str) -> int:
        name = format_name_for(meeting_type)
        if name not in self._format_ids:
            format_id = len(self.meeting_formats) + 1
            self._format_ids[name] = format_id
            self.meeting_formats.append({"format_id": format_id, "name": name, "description": None})
        return self._format_ids[name]

    def _project_id(self, name: str) -> int:
        if name not in self._project_ids:
            project_id = len(self.projects) + 1
            self._project_ids[name] = project_id
            self.projects.append({
                "project_id": project_id,
                "project_name": name,
                "description": None,
                "start_year": None,
                "status": "Active",
            })
        return self._project_ids[name]

    def _location_id(
        self,
        name: str,
        *,
        location_type: str = "General Area",
        address: str = "",
        latitude: object = None,
        longitude: object = None,
    ) -> int:
        if name not in self._location_ids:
            location_id = len(self.locations) + 1
            self._location_ids[name] = location_id
            self.locations.append({
                "location_id": location_id,
                "location_name": name,
                "location_type": location_type,
                "address": address,
                "description": None,
                "latitude": latitude,
                "longitude": longitude,
            })
        return self._location_ids[name]

    def _location_by_id(self, location_id: int) -> dict | None:
        return next((loc for loc in self.locations if loc["location_id"] == location_id), None)

    def _add_location_v2_for_seed(self, item_id: int, location_id: int) -> None:
        loc = self._location_by_id(location_id)
        if not loc:
            return
        seed = LOCATION_SEEDS.get(str(loc.get("location_name") or ""), {})
        has_coordinates = loc.get("latitude") not in (None, "") and loc.get("longitude") not in (None, "")
        geocode_confidence = seed.get("confidence")
        if geocode_confidence is None:
            geocode_confidence = 0.75 if has_coordinates else 0.4
        self._add_location_v2(
            item_id=item_id,
            address_raw=str(loc.get("address") or loc.get("location_name") or ""),
            address_normalized=str(loc.get("address") or ensure_estero_address(str(loc.get("location_name") or ""))),
            latitude=loc.get("latitude"),
            longitude=loc.get("longitude"),
            geocode_confidence=float(geocode_confidence),
            location_name=str(loc.get("location_name") or ""),
            project_name=(self._legacy_location_lookup.get(str(loc.get("location_name") or "").lower()) or {}).get("project_name"),
        )

    def _add_location_v2(
        self,
        *,
        item_id: int,
        address_raw: str,
        address_normalized: str,
        latitude: object,
        longitude: object,
        geocode_confidence: float,
        location_name: str | None = None,
        project_name: str | None = None,
        location_type: str | None = None,
        resolution_notes: str | None = None,
        location_seq: int | None = None,
        is_primary: bool | None = None,
        parcel_id: str | None = None,
    ) -> None:
        legacy = self._legacy_location_lookup.get((location_name or "").lower())
        if not legacy:
            legacy = self._legacy_location_lookup.get(address_raw.lower()) or self._legacy_location_lookup.get(address_normalized.lower())
        if legacy:
            latitude = latitude or legacy.get("latitude")
            longitude = longitude or legacy.get("longitude")
            project_name = project_name or legacy.get("project_name")
            location_name = location_name or legacy.get("location_name")
            if geocode_confidence < 0.9:
                geocode_confidence = 0.9
        key = (item_id, address_normalized.lower())
        for row in self.locations_v2:
            if row["item_id"] == item_id and str(row["address_normalized"]).lower() == key[1]:
                return
        # Auto-assign location_seq: 1 for first row of an item, then increment.
        # Callers that pass an explicit seq override (e.g. resolve_all fan-out)
        # take precedence so multi-site ordering is stable across re-runs.
        if location_seq is None:
            existing_for_item = sum(1 for r in self.locations_v2 if r["item_id"] == item_id)
            location_seq = existing_for_item + 1
        if is_primary is None:
            is_primary = location_seq == 1
        self.locations_v2.append({
            "location_id": len(self.locations_v2) + 1,
            "item_id": item_id,
            "address_raw": address_raw,
            "address_normalized": address_normalized,
            "latitude": latitude,
            "longitude": longitude,
            "parcel_id": parcel_id,
            "geocode_confidence": geocode_confidence,
            "created_at": None,
            "location_name": location_name or address_raw,
            "project_name": project_name,
            "location_type": location_type or "",
            "resolution_notes": resolution_notes or "",
            "location_seq": location_seq,
            "is_primary": "true" if is_primary else "false",
        })

    def _add_location_candidate_for_seed(
        self,
        item_id: int,
        meeting_id: int,
        location_id: int,
        location_name: str,
        evidence: str,
    ) -> None:
        loc = self._location_by_id(location_id)
        if not loc:
            return
        if loc.get("latitude") not in (None, "") and loc.get("longitude") not in (None, ""):
            return
        self._add_location_candidate(
            item_id=item_id,
            meeting_id=meeting_id,
            location_id=location_id,
            candidate_name=location_name,
            candidate_address=str(loc.get("address") or ensure_estero_address(location_name)),
            candidate_source="known_location_missing_coordinates",
            evidence=evidence,
        )

    def _add_location_candidate(
        self,
        *,
        item_id: int,
        meeting_id: int,
        location_id: int,
        candidate_name: str,
        candidate_address: str,
        candidate_source: str,
        evidence: str,
    ) -> None:
        for row in self.location_candidates:
            if (
                row["item_id"] == item_id
                and row["location_id"] == location_id
                and row["candidate_address"].lower() == candidate_address.lower()
            ):
                return
        self.location_candidates.append({
            "candidate_id": len(self.location_candidates) + 1,
            "item_id": item_id,
            "meeting_id": meeting_id,
            "location_id": location_id,
            "candidate_name": candidate_name,
            "candidate_address": candidate_address,
            "candidate_source": candidate_source,
            "arcgis_query": candidate_address or ensure_estero_address(candidate_name),
            "evidence": evidence[:500],
            "review_status": "Needs Geocode",
        })

    def _category_id(self, name: str, description: str | None = None) -> int:
        if name not in self._category_ids:
            category_id = len(self.agenda_categories) + 1
            self._category_ids[name] = category_id
            self.agenda_categories.append({
                "category_id": category_id,
                "category_name": name,
                "description": description,
            })
        elif description:
            for category in self.agenda_categories:
                if category["category_id"] == self._category_ids[name] and not category.get("description"):
                    category["description"] = description
                    break
        return self._category_ids[name]

    def _venue_address(self, venue_name: str) -> str | None:
        if venue_name == "Estero Village Hall":
            return "9401 Corkscrew Palms Circle, Estero, FL 33928"
        if venue_name == "Legacy Church":
            return "21115 Design Parc Lane, Estero, FL 33928"
        if venue_name == "Estero Fire Rescue":
            return "21500 Three Oaks Parkway, Estero, FL 33928"
        return None

    def _venue_location_id(self, venue_name: str) -> int:
        seed = LOCATION_SEEDS.get(venue_name, {})
        return self._location_id(
            venue_name,
            location_type="Meeting Venue",
            address=str(seed.get("address") or self._venue_address(venue_name) or ""),
            latitude=seed.get("latitude"),
            longitude=seed.get("longitude"),
        )

    def _add_review(self, asset: PdfAsset, meeting_id: int, needs_ocr: bool, reason: str) -> None:
        self._add_review_filename(asset.filename, meeting_id, needs_ocr, reason)

    def _add_review_filename(self, filename: str, meeting_id: int, needs_ocr: bool, reason: str) -> None:
        self.review_rows.append({
            "filename": filename,
            "meeting_id": meeting_id,
            "needs_ocr": needs_ocr,
            "reason": reason,
        })

    def write(self, out_dir: Path) -> None:
        # Medallion layout, matching the legacy EagleGIS repo's bronze/silver/
        # gold tiers (and the consumer contract in rag-arcgis-chatbot's
        # sync-data.yml, which pulls gold/meetings_ai_public.csv):
        #   bronze/ — hand-curated inputs (geocode overrides; never regenerated)
        #   silver/ — validated relational tables (core/ + v2/) and QA triage
        #             outputs (review/)
        #   gold/   — publication-ready deliverables: the AI-ready flat CSV and
        #             the ArcGIS map exports + per-category layers
        bronze_dir = out_dir / "bronze"
        silver_dir = out_dir / "silver"
        core_dir = silver_dir / "core"
        v2_dir = silver_dir / "v2"
        review_dir = silver_dir / "review"
        gold_dir = out_dir / "gold"
        arcgis_dir = gold_dir / "arcgis"
        layers_dir = arcgis_dir / "layers"

        self._apply_geocode_cache(bronze_dir / "geocoded_locations.csv")

        write_csv(core_dir / "boards.csv", self.boards, [
            "board_id", "code", "name", "active_from", "active_to",
        ])
        write_csv(core_dir / "meeting_formats.csv", self.meeting_formats, [
            "format_id", "name", "description",
        ])
        write_csv(core_dir / "meeting_types.csv", self.meeting_types, ["type_id", "type_name", "description"])
        meeting_fields = [
            "meeting_id", "board_id", "format_id", "legacy_meeting_id", "title",
            "meeting_date", "meeting_time", "meeting_location", "pdf_url", "raw_text",
            "summary", "status", "filename", "type_id", "meeting_year",
            "venue_location_id", "venue_name", "venue_address", "start_time",
            "end_time", "notes",
        ]
        write_csv(core_dir / "meetings.csv", self.meetings, meeting_fields)
        write_csv(v2_dir / "meetings_v2.csv", self.meetings, meeting_fields)
        write_csv(core_dir / "documents.csv", self.documents, [
            "document_id", "meeting_id", "title", "document_type", "file_name",
            "file_url", "upload_date", "doc_date", "notes",
        ])
        write_csv(v2_dir / "documents_v2.csv", self.documents_v2, [
            "document_id", "meeting_id", "legacy_document_id", "document_type",
            "title", "file_url", "uploaded_at",
        ])
        write_csv(core_dir / "projects.csv", self.projects, [
            "project_id", "project_name", "description", "start_year", "status",
        ])
        write_csv(core_dir / "locations.csv", self.locations, [
            "location_id", "location_name", "location_type", "address",
            "description", "latitude", "longitude",
        ])
        write_csv(core_dir / "legacy_locations.csv", self.locations, [
            "location_id", "location_name", "location_type", "address",
            "description", "latitude", "longitude",
        ])
        write_csv(core_dir / "agenda_categories.csv", self.agenda_categories, [
            "category_id", "category_name", "description",
        ])
        write_csv(core_dir / "agenda_items.csv", self.agenda_items, [
            "item_id", "meeting_id", "item_number", "item_type", "application_id",
            "applicant_name", "project_title", "district", "address_raw", "summary",
            "outcome", "motion_text", "vote_result", "created_at",
            "project_matches", "staff_code", "needs_review", "extraction_confidence",
            "category_id", "item_order", "item_title", "item_text", "action_taken",
            "action_type", "vote_detected", "extraction_notes",
        ])
        write_csv(core_dir / "agenda_item_projects.csv", self.agenda_item_projects, [
            "item_id", "project_id",
        ])
        write_csv(core_dir / "agenda_item_locations.csv", self.agenda_item_locations, [
            "item_id", "location_id",
        ])
        write_csv(v2_dir / "locations_v2.csv", self.locations_v2, [
            "location_id", "item_id", "address_raw", "address_normalized",
            "latitude", "longitude", "parcel_id", "geocode_confidence", "created_at",
            "location_name", "project_name", "location_type", "resolution_notes",
            "location_seq", "is_primary",
        ])
        write_csv(core_dir / "motions.csv", self.motions, [
            "motion_id", "item_id", "motion_text", "proposed_by", "seconded_by",
            "outcome", "vote_yes", "vote_no", "vote_abstain", "created_at",
        ])
        agenda_arcgis_rows, missing_coordinate_rows = self._build_agenda_arcgis_rows()
        arcgis_fields = [
            "ProjectName", "LayerCategory", "CategoryID", "Board", "MeetingFormat", "MeetingType", "MeetingDate",
            "ArcGIS_Date", "MeetingYear", "Status", "AgendaItemID", "AgendaItemNumber",
            "AgendaItemType", "ProjectTitle", "Summary", "ActionTaken", "Outcome",
            "MotionText", "ProposedBy", "SecondedBy", "VoteResult", "ApplicantName",
            "ApplicationID", "District", "LocationName", "Location", "Latitude",
            "Longitude", "GeocodeConfidence", "StaffCode", "Filename", "Document_Link",
            "RecordType", "LocationSeq", "IsPrimary", "ParcelID",
        ]
        write_csv(arcgis_dir / "arcgis_agenda_map_data.csv", agenda_arcgis_rows, arcgis_fields)
        self._write_category_csvs(layers_dir, agenda_arcgis_rows, arcgis_fields)
        write_csv(arcgis_dir / "arcgis_missing_coordinates.csv", missing_coordinate_rows, [
            "AgendaItemID", "MeetingDate", "ProjectTitle", "LocationName", "Location",
            "ActionTaken", "Document_Link",
        ])
        write_csv(review_dir / "location_candidates.csv", self.location_candidates, [
            "candidate_id", "item_id", "meeting_id", "location_id", "candidate_name",
            "candidate_address", "candidate_source", "arcgis_query", "evidence",
            "review_status",
        ])
        write_csv(review_dir / "unmapped_agenda_items.csv", self.unmapped_agenda_items, [
            "item_id", "meeting_id", "action_type", "vote_detected",
            "project_matches", "review_reason", "item_text",
        ])
        write_csv(review_dir / "extraction_review.csv", self.review_rows, [
            "filename", "meeting_id", "needs_ocr", "reason",
        ])
        write_csv(
            gold_dir / "meetings_ai_public.csv",
            build_ai_public_rows(self._gold_tables()),
            AI_PUBLIC_FIELDS,
        )

    def _gold_tables(self) -> dict[str, list[dict]]:
        return {
            "boards": self.boards,
            "meeting_formats": self.meeting_formats,
            "meeting_types": self.meeting_types,
            "meetings": self.meetings,
            "agenda_categories": self.agenda_categories,
            "agenda_items": self.agenda_items,
            "agenda_item_projects": self.agenda_item_projects,
            "projects": self.projects,
            "motions": self.motions,
            "locations_v2": self.locations_v2,
        }

    def _write_category_csvs(
        self,
        layers_dir: Path,
        arcgis_rows: list[dict],
        fields: list[str],
    ) -> None:
        layers_dir.mkdir(parents=True, exist_ok=True)
        by_category: dict[str, list[dict]] = {}
        for row in arcgis_rows:
            cat = row.get("LayerCategory") or "Uncategorized"
            by_category.setdefault(cat, []).append(row)
        for category, rows in by_category.items():
            filename = re.sub(r"[^a-z0-9]+", "_", category.lower()).strip("_") + ".csv"
            write_csv(layers_dir / filename, rows, fields)

    def _build_agenda_arcgis_rows(self) -> tuple[list[dict], list[dict]]:
        meetings = {str(row["meeting_id"]): row for row in self.meetings}
        boards = {str(row["board_id"]): row for row in self.boards}
        formats = {str(row["format_id"]): row for row in self.meeting_formats}
        categories = {str(row["category_id"]): row for row in self.agenda_categories}
        motions_by_item: dict[str, dict] = {}
        for motion in self.motions:
            motions_by_item.setdefault(str(motion["item_id"]), motion)

        rows: list[dict] = []
        missing: list[dict] = []
        for item in self.agenda_items:
            item_locations = [loc for loc in self.locations_v2 if str(loc["item_id"]) == str(item["item_id"])]
            if should_suppress_arcgis_item(item, item_locations):
                continue
            if not item_locations:
                continue
            meeting = meetings.get(str(item["meeting_id"]), {})
            board = boards.get(str(meeting.get("board_id")), {})
            meeting_format = formats.get(str(meeting.get("format_id")), {})
            category = categories.get(str(item.get("category_id")), {})
            motion = motions_by_item.get(str(item["item_id"]), {})
            project_name = item.get("project_matches") or ""

            for loc in item_locations:
                row = {
                    "ProjectName": arcgis_text(loc.get("project_name") or project_name or item.get("project_title"), 250),
                    "LayerCategory": category.get("category_name") or "Uncategorized",
                    "CategoryID": item.get("category_id"),
                    "Board": board.get("name"),
                    "MeetingFormat": meeting_format.get("name"),
                    "MeetingType": meeting.get("summary"),
                    "MeetingDate": meeting.get("meeting_date"),
                    "ArcGIS_Date": meeting.get("meeting_date"),
                    "MeetingYear": str(meeting.get("meeting_date") or "")[:4],
                    "Status": meeting.get("status"),
                    "AgendaItemID": item.get("item_id"),
                    "AgendaItemNumber": item.get("item_number"),
                    "AgendaItemType": item.get("item_type"),
                    "ProjectTitle": arcgis_text(item.get("project_title"), 250),
                    "Summary": arcgis_text(item.get("summary"), 500),
                    "ActionTaken": arcgis_text(item.get("outcome"), 1000),
                    "Outcome": arcgis_text(item.get("outcome"), 1000),
                    "MotionText": arcgis_text(motion.get("motion_text") or item.get("motion_text"), 1000),
                    "ProposedBy": motion.get("proposed_by"),
                    "SecondedBy": motion.get("seconded_by"),
                    "VoteResult": item.get("vote_result"),
                    "ApplicantName": arcgis_text(item.get("applicant_name"), 250),
                    "ApplicationID": item.get("application_id"),
                    "District": item.get("district"),
                    "LocationName": arcgis_text(loc.get("location_name"), 250),
                    "Location": arcgis_text(loc.get("address_normalized") or loc.get("address_raw"), 250),
                    "Latitude": loc.get("latitude"),
                    "Longitude": loc.get("longitude"),
                    "GeocodeConfidence": loc.get("geocode_confidence"),
                    "StaffCode": item.get("staff_code"),
                    "Filename": meeting.get("filename"),
                    "Document_Link": meeting.get("pdf_url"),
                    "RecordType": "AgendaItemLocation",
                    "LocationSeq": loc.get("location_seq"),
                    "IsPrimary": loc.get("is_primary"),
                    "ParcelID": loc.get("parcel_id"),
                }
                if row["Latitude"] not in (None, "") and row["Longitude"] not in (None, ""):
                    rows.append(row)
                else:
                    missing.append({
                        "AgendaItemID": item.get("item_id"),
                        "MeetingDate": meeting.get("meeting_date"),
                        "ProjectTitle": item.get("project_title"),
                        "LocationName": loc.get("location_name"),
                        "Location": loc.get("address_normalized") or loc.get("address_raw"),
                        "ActionTaken": item.get("outcome"),
                        "Document_Link": meeting.get("pdf_url"),
                    })
        return sorted(rows, key=arcgis_sort_key), sorted(missing, key=arcgis_sort_key)

    def _apply_geocode_cache(self, cache_path: Path) -> None:
        if not cache_path.exists():
            return
        with cache_path.open(encoding="utf-8") as handle:
            cached = {
                normalize_geocode_key(row["Location"]): row
                for row in csv.DictReader(handle)
                if row.get("Location") and row.get("Latitude") and row.get("Longitude")
            }
        if not cached:
            return
        for row in self.locations_v2:
            key = row.get("address_normalized") or row.get("address_raw")
            hit = cached.get(normalize_geocode_key(key))
            if not hit:
                continue
            try:
                row_confidence = float(row.get("geocode_confidence") or 0)
            except (TypeError, ValueError):
                row_confidence = 0.0
            try:
                hit_confidence = float(hit.get("GeocodeConfidence") or 0)
            except (TypeError, ValueError):
                hit_confidence = 0.0
            if row_confidence >= 0.95:
                continue
            if row.get("latitude") not in (None, "") and row.get("longitude") not in (None, "") and row_confidence > hit_confidence:
                continue
            row["latitude"] = hit["Latitude"]
            row["longitude"] = hit["Longitude"]
            row["geocode_confidence"] = hit.get("GeocodeConfidence") or row.get("geocode_confidence")


def arcgis_sort_key(row: dict) -> tuple:
    return (
        str(row.get("LayerCategory") or ""),
        str(row.get("LocationName") or ""),
        str(row.get("ProjectName") or ""),
        str(row.get("ArcGIS_Date") or row.get("MeetingDate") or ""),
        int(row.get("AgendaItemID") or 0),
        str(row.get("AgendaItemNumber") or ""),
    )


def infer_vote_result(text: str) -> str | None:
    lo = text.lower()
    if "unanimous" in lo:
        return "Unanimous"
    if "nay:" in lo or "nay " in lo:
        return "See minutes"
    if vote_detected(text):
        return "Detected"
    return None


def infer_vote_counts(text: str) -> tuple[int | None, int | None, int | None]:
    yes = _count_vote_names(text, "Aye")
    no = _count_vote_names(text, "Nay")
    abstain = _count_vote_names(text, "Abstentions")
    return yes, no, abstain


def _count_vote_names(text: str, label: str) -> int | None:
    match = re.search(
        rf"\b{re.escape(label)}\s*:\s*(.*?)(?=\s*(?:(?:\d{{1,2}}\.\s+)?(?:Aye|Nay|Abstentions|Motion|Action|Vote|Public Input|Board Communications|Adjournment)\s*:|(?:\([a-z0-9]\)|\d{{1,2}}\.)\s+[A-Z]|Planning Zoning and Design Board Minutes|Final Action Agenda|Recess\b|$))",
        text,
        flags=re.I,
    )
    if not match:
        return None
    value = match.group(1).strip(" .;:")
    if not value:
        return 0
    if value.lower() in {"none", "n/a", "na"}:
        return 0
    if re.match(
        r"^(?:\([a-z0-9]\)|\d{1,2}\.)\s+[A-Z]|^(?:Planning Zoning and Design Board Minutes|Final Action Agenda|Recess\b)",
        value,
        flags=re.I,
    ):
        return 0
    value = re.sub(r"\b(?:board\s+members?|chairman|chair|vice\s+chairman|vice\s+chair|co-chairman|and)\b", "", value, flags=re.I)
    parts = [part.strip(" .;:") for part in re.split(r",|;", value) if part.strip(" .;:")]
    return len(parts) if parts else None


def format_name_for(meeting_type: str) -> str:
    if meeting_type == "Village Council Regular Meeting":
        return "Regular Meeting"
    if meeting_type == "Special Emergency Meeting":
        return "Emergency Meeting"
    if meeting_type == "Special Meeting":
        return "Special Meeting"
    if meeting_type == "Workshop":
        return "Workshop"
    if meeting_type == "Joint Workshop":
        return "Joint Workshop"
    if meeting_type == "Zoning Hearing":
        return "Zoning Hearing"
    if meeting_type == "Public Hearing":
        return "Public Hearing"
    if meeting_type == "Budget Hearing":
        return "Budget Hearing"
    if meeting_type == "Organizational Meeting":
        return "Organizational Meeting"
    if meeting_type == "Combined Zoning Hearing / Workshop":
        return "Combined Hearing / Workshop"
    if meeting_type == "Comprehensive Plan Workshop":
        return "Comprehensive Plan Workshop"
    if meeting_type == "Cancelled Meeting":
        return "Cancelled"
    if meeting_type == "Public Information Meeting":
        return "Public Information Meeting"
    if meeting_type == "Planning Zoning & Design Board":
        return "Regular Meeting"
    return meeting_type or "Regular Meeting"


def grouped_meeting_type_for(meeting_type: str, filename: str = "") -> str:
    value = normalize_meeting_type(meeting_type) or meeting_type or ""
    lo = value.lower()
    if "planning zoning" in lo or PZDB_FILENAME_RE.search(filename or ""):
        return "Planning Zoning & Design Board"
    if "workshop" in lo or "public information" in lo or "open house" in lo:
        return "Workshop"
    if "hearing" in lo or "zoning" in lo:
        return "Public Hearing"
    return "Village Council"


def infer_motion_text(text: str) -> str | None:
    match = re.search(r"(Motion:.*?)(?:Action:|Vote:|$)", text, flags=re.I)
    return match.group(1).strip() if match else None


def infer_motion_person(text: str, label: str) -> str | None:
    match = re.search(rf"{re.escape(label)}:\s*(.*?)(?=\s+(?:Seconded by|Action:|Vote:|$))", text, flags=re.I)
    return match.group(1).strip(" .;:") if match else None


def site_location_override(application_id: str | None, text: str) -> dict[str, object] | None:
    if application_id and application_id in SITE_LOCATION_OVERRIDES:
        return SITE_LOCATION_OVERRIDES[application_id]
    lo = text.lower()
    for override in SITE_TEXT_LOCATION_OVERRIDES:
        marker = str(override.get("text") or "").lower()
        if marker and marker in lo:
            return override
    return None


def infer_application_id(text: str) -> str | None:
    patterns = [
        r"\b((?:DOS|LDO|DCI|COP|ADD|CPA|ZTA|DO)\s*\d{4}-[A-Z]?\d{3})\b",
        r"\b((?:RFB|RFQ|CN|EC|STA)\s+(?:No\.\s*)?[A-Z0-9-]+)\b",
        r"\b((?:Resolution|Ordinance)\s+(?:No\.\s*)?\d{4}-\d{1,3})\b",
        r"\b([A-Z]{2,5}\s+\d{4}-\d{1,3})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            value = re.sub(r"\s+", " ", match.group(1)).strip()
            if _valid_application_id(value):
                return value
    return None


def _valid_application_id(value: str) -> bool:
    if len(value) < 6:
        return False
    return bool(re.search(r"\d{4}-[A-Z]?\d{1,3}", value) or re.search(r"\b(?:RFB|RFQ|CN|EC|STA)\s+", value, re.I))


def infer_applicant_name(text: str) -> str | None:
    patterns = [
        r"\bApplicant:\s*(.*?)(?=\s+(?:Staff|Council|Public|Motion|Action|Vote):|$)",
        r"\bwith\s+([A-Z][A-Za-z0-9&.,' -]+?)(?:\s+to\s+|\s+for\s+|,|\.)",
        r"\bfrom\s+([A-Z][A-Za-z0-9&.,' -]+?)(?:,|\s+in\s+|\s+for\s+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = re.sub(r"\s+", " ", match.group(1)).strip(" .;:")
            if 2 < len(value) <= 120:
                return value
    return None


def infer_district(text: str) -> int | None:
    match = re.search(r"\bDistrict\s+(\d+)\b", text, flags=re.I)
    return int(match.group(1)) if match else None


def first_address_or_location(text: str, location_names: list[str]) -> str | None:
    addresses = extract_address_candidates(text)
    if addresses:
        return ensure_estero_address(normalize_address_candidate(addresses[0]))
    return location_names[0] if location_names else None


def arcgis_text(value: object, limit: int) -> object:
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def is_arcgis_header_spillover(item: dict) -> bool:
    text = f"{item.get('project_title') or ''} {item.get('summary') or ''} {item.get('outcome') or ''}".lower()
    bad_starts = (
        "approved minutes",
        "approved as presented:",
        "approved as submitted:",
        "approved by council",
        "approved in item",
        "adopted prior to december 31, 2014",
        "adopted rules or policies",
        "no action required",
        "council communications",
        "village manager comments",
        "village manager's comments",
        "village attorney comments",
        "village attorney's comments",
    )
    if any(text.startswith(prefix) for prefix in bad_starts):
        return True
    return "call to order" in text[:1200] and ("roll call" in text[:1400] or "rollcall" in text[:1400])


def is_non_spatial_arcgis_item(item: dict) -> bool:
    text = f"{item.get('project_title') or ''} {item.get('summary') or ''} {item.get('outcome') or ''}".lower()
    non_spatial_markers = (
        "bert harris lawsuits",
        "senior homestead exemption",
        "hydraulic fracturing",
        "well stimulation",
        "fracking",
        "village council liaison assignments",
        "liaison assignments to outside organizations",
    )
    return any(marker in text for marker in non_spatial_markers)


def should_suppress_arcgis_item(item: dict, item_locations: list[dict]) -> bool:
    if is_arcgis_header_spillover(item):
        return True
    if is_non_spatial_arcgis_item(item):
        return True

    action_type = str(item.get("item_type") or "")
    if action_type not in {"No Action", "Unknown", "Public Comment", "Discussion"}:
        return False
    if len(item_locations) < 2:
        return False

    text = f"{item.get('project_title') or ''} {item.get('summary') or ''} {item.get('outcome') or ''}".lower()
    narrative_markers = (
        "public input on non-agenda items",
        "public input on any issue",
        "council communications and future agenda items",
        "council communications / future agenda items",
        "council communications i future agenda items",
        "board communications",
        "village manager comments",
        "village manager's comments",
        "village attorney comments",
        "village attorney's comments",
        "adjourn a motion to adjourn",
        "adjourned the meeting",
    )
    return any(marker in text for marker in narrative_markers)


def ensure_estero_address(value: str) -> str:
    if not value:
        return "Estero, FL"
    if re.search(r",\s*[^,]+,\s*(?:fl|florida)\b", value, flags=re.I):
        return value
    if re.search(r",\s*estero\b", value, flags=re.I):
        return value
    return f"{value}, Estero, FL"


def normalize_address_candidate(value: str) -> str:
    value = value.replace("Design Pare Lane", "Design Parc Lane")
    value = re.sub(r"\b0251\s+Arcos\s+Avenue\b", "10251 Arcos Avenue", value, flags=re.I)
    return value


def filter_address_candidates(
    addresses: list[str],
    *,
    text: str,
    action_type: str,
    application_id: str | None,
    project_names: list[str],
) -> list[str]:
    if not addresses:
        return []

    # Contract award narratives often embed vendor office addresses that are not
    # the agenda item's map target.
    if (
        action_type == "Contract Approval"
        and len(addresses) > 1
        and not application_id
        and not project_names
        and re.search(r"\b(?:Fort Myers|Bonita Springs)\b", text, flags=re.I)
    ):
        return []

    filtered: list[str] = []
    for address in addresses:
        lo = address.lower()
        if re.match(r"^(?:19|20)\d{2}\s+to\b", lo):
            continue
        if "office of green ways" in lo or "office of greenways" in lo:
            continue
        if address not in filtered:
            filtered.append(address)
    return filtered


def normalize_geocode_key(value: object) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("&", " and ")
    text = text.replace("#", " ")
    text = re.sub(r"[.,]", "", text)
    text = re.sub(r"\b(and)\b", "and", text)
    replacements = {
        r"\bsouth\b": "s",
        r"\bnorth\b": "n",
        r"\beast\b": "e",
        r"\bwest\b": "w",
        r"\bavenue\b": "ave",
        r"\bstreet\b": "st",
        r"\broad\b": "rd",
        r"\broadway avenue west\b": "broadway ave w",
        r"\broadway avenue east\b": "broadway ave e",
        r"\bboulevard\b": "blvd",
        r"\bparkway\b": "pkwy",
        r"\blane\b": "ln",
        r"\bdrive\b": "dr",
        r"\bcourt\b": "ct",
        r"\bcircle\b": "cir",
        r"\bplace\b": "pl",
        r"\btrail\b": "trl",
    }
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def confidence_score(
    review: bool,
    needs_ocr_flag: bool,
    used_csv_fallback: bool,
    project_names: list[str],
    location_names: list[str],
) -> float:
    score = 0.95
    if review:
        score -= 0.2
    if needs_ocr_flag:
        score -= 0.35
    if used_csv_fallback:
        score -= 0.15
    if not project_names:
        score -= 0.15
    if not location_names:
        score -= 0.1
    return max(0.05, round(score, 2))


if __name__ == "__main__":
    main()

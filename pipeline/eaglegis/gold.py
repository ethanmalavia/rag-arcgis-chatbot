"""Gold-tier export: the flat, AI-ready meetings CSV.

Produces gold/meetings_ai_public.csv in the 52-column contract consumed by
the rag-arcgis-chatbot backend (sync-data.yml copies it to
backend/data/data.csv). One row per agenda item; the item's primary resolved
location is denormalized onto the row when one exists.

Column order and names must not change without coordinating with the
chatbot backend.
"""
from __future__ import annotations

import re

AI_PUBLIC_FIELDS = [
    "RecordId", "SourceBoard", "DataGrain", "RecordType", "MeetingId",
    "ItemId", "MotionId", "Board", "MeetingFormat", "MeetingType",
    "MeetingDate", "MeetingYear", "MeetingTime", "MeetingVenue", "Status",
    "AgendaItemNumber", "AgendaItemType", "FactCategory", "LandUseCategory",
    "ProjectName", "ProjectTitle", "Summary", "ApplicationId",
    "ApplicationType", "ApplicantName", "District", "StaffCode",
    "ActionTaken", "Outcome", "MotionText", "ProposedBy", "SecondedBy",
    "VoteResult", "VoteYes", "VoteNo", "VoteAbstain", "AddressRaw",
    "AddressNormalized", "LocationName", "Latitude", "Longitude",
    "GeocodeConfidence", "LocationGrain", "ParcelId", "PrimarySourceUrl",
    "SourceFilename", "ExtractionMethod", "ExtractionConfidence",
    "ReviewRequired", "ReviewReason", "AiReady", "CitationText",
]

_APPLICATION_CODE = re.compile(r"\b(DOS|DCI|LDO|ADD|CPA|REZ)\d*", re.I)


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _slug(value) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _text(value).lower()).strip("_")


def _application_type(application_id: str) -> str:
    lo = application_id.lower()
    if lo.startswith("ordinance"):
        return "ordinance"
    if lo.startswith("resolution"):
        return "resolution"
    match = _APPLICATION_CODE.search(application_id)
    if match:
        return match.group(1).lower()
    return ""


def _primary_location(locations: list[dict]) -> dict | None:
    if not locations:
        return None
    for loc in locations:
        if _truthy(loc.get("is_primary")):
            return loc
    return min(locations, key=lambda loc: int(loc.get("location_seq") or 0))


def build_ai_public_rows(tables: dict[str, list[dict]]) -> list[dict]:
    boards = {row["board_id"]: row for row in tables.get("boards", [])}
    formats = {row["format_id"]: row for row in tables.get("meeting_formats", [])}
    types = {row["type_id"]: row for row in tables.get("meeting_types", [])}
    meetings = {row["meeting_id"]: row for row in tables.get("meetings", [])}
    categories = {row["category_id"]: row for row in tables.get("agenda_categories", [])}
    projects = {row["project_id"]: row for row in tables.get("projects", [])}

    project_by_item: dict = {}
    for link in tables.get("agenda_item_projects", []):
        project = projects.get(link.get("project_id"))
        if project and link.get("item_id") not in project_by_item:
            project_by_item[link["item_id"]] = project

    motion_by_item: dict = {}
    for motion in tables.get("motions", []):
        existing = motion_by_item.get(motion.get("item_id"))
        if existing is None or int(motion.get("motion_id") or 0) < int(existing.get("motion_id") or 0):
            motion_by_item[motion["item_id"]] = motion

    locations_by_item: dict = {}
    for loc in tables.get("locations_v2", []):
        locations_by_item.setdefault(loc.get("item_id"), []).append(loc)

    rows: list[dict] = []
    for item in tables.get("agenda_items", []):
        meeting = meetings.get(item.get("meeting_id")) or {}
        board = boards.get(meeting.get("board_id")) or {}
        board_code = _text(board.get("code")).lower()
        category = categories.get(item.get("category_id")) or {}
        project = project_by_item.get(item.get("item_id"))
        motion = motion_by_item.get(item.get("item_id"))
        location = _primary_location(locations_by_item.get(item.get("item_id"), []))

        application_id = _text(item.get("application_id"))
        action = _text(item.get("action_taken")) or _text(item.get("outcome"))
        review_required = _truthy(item.get("needs_review"))
        source_url = _text(meeting.get("pdf_url"))
        item_number = _text(item.get("item_number"))
        item_type = _text(item.get("item_type"))
        board_name = _text(board.get("name"))
        meeting_date = _text(meeting.get("meeting_date"))

        citation = ""
        if action:
            item_ref = f", agenda item {item_number}" if item_number else ""
            type_ref = f" ({item_type})" if item_type else ""
            citation = f"{board_name} meeting on {meeting_date}{item_ref}{type_ref}: {action}"
            if source_url:
                citation += f" Source: {source_url}"

        rows.append({
            "RecordId": f"{board_code}:{item.get('item_id')}",
            "SourceBoard": board_code,
            "DataGrain": "agenda_item",
            "RecordType": "AgendaItemLocation" if location else "AgendaItem",
            "MeetingId": item.get("meeting_id"),
            "ItemId": item.get("item_id"),
            "MotionId": motion.get("motion_id") if motion else "",
            "Board": board_name,
            "MeetingFormat": _text((formats.get(meeting.get("format_id")) or {}).get("name")),
            "MeetingType": _text((types.get(meeting.get("type_id")) or {}).get("type_name")),
            "MeetingDate": meeting_date,
            "MeetingYear": _text(meeting.get("meeting_year")),
            "MeetingTime": _text(meeting.get("meeting_time")),
            "MeetingVenue": _text(meeting.get("venue_name")),
            "Status": _text(meeting.get("status")),
            "AgendaItemNumber": item_number,
            "AgendaItemType": item_type,
            "FactCategory": _slug(item.get("action_type")),
            "LandUseCategory": _slug(category.get("category_name")),
            "ProjectName": _text(project.get("project_name")) if project else "",
            "ProjectTitle": _text(item.get("project_title")),
            "Summary": _text(item.get("summary")),
            "ApplicationId": application_id,
            "ApplicationType": _application_type(application_id),
            "ApplicantName": _text(item.get("applicant_name")),
            "District": _text(item.get("district")),
            "StaffCode": _text(item.get("staff_code")),
            "ActionTaken": _text(item.get("action_taken")),
            "Outcome": _text(item.get("outcome")),
            "MotionText": _text(item.get("motion_text")),
            "ProposedBy": _text(motion.get("proposed_by")) if motion else "",
            "SecondedBy": _text(motion.get("seconded_by")) if motion else "",
            "VoteResult": _text(item.get("vote_result")),
            "VoteYes": _text(motion.get("vote_yes")) if motion else "",
            "VoteNo": _text(motion.get("vote_no")) if motion else "",
            "VoteAbstain": _text(motion.get("vote_abstain")) if motion else "",
            "AddressRaw": _text(location.get("address_raw")) if location else _text(item.get("address_raw")),
            "AddressNormalized": _text(location.get("address_normalized")) if location else "",
            "LocationName": _text(location.get("location_name")) if location else "",
            "Latitude": _text(location.get("latitude")) if location else "",
            "Longitude": _text(location.get("longitude")) if location else "",
            "GeocodeConfidence": _text(location.get("geocode_confidence")) if location else "",
            "LocationGrain": _slug(location.get("location_type")) if location else "",
            "ParcelId": _text(location.get("parcel_id")) if location else "",
            "PrimarySourceUrl": source_url,
            "SourceFilename": _text(meeting.get("filename")),
            "ExtractionMethod": f"pdf_extraction:{board_code}" if board_code else "pdf_extraction",
            "ExtractionConfidence": _text(item.get("extraction_confidence")),
            "ReviewRequired": "true" if review_required else "false",
            "ReviewReason": _text(item.get("extraction_notes")),
            "AiReady": "true" if (action and not review_required) else "false",
            "CitationText": citation,
        })
    return rows

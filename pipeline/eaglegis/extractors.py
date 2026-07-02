from __future__ import annotations

from dataclasses import dataclass
import re
from datetime import datetime
from pathlib import Path


DATE_FORMATS = ("%B %d %Y", "%b %d %Y", "%Y-%m-%d")

# Meeting dates outside this window are regex false positives (the Village
# incorporated Dec 31, 2014).
MEETING_YEAR_MIN = 2014
MEETING_YEAR_MAX = 2035


MEETING_TYPE_ALIASES = {
    "regular council meeting": "Village Council Regular Meeting",
    "village council meeting": "Village Council Regular Meeting",
    "village council regular meeting": "Village Council Regular Meeting",
    "pzdb meeting": "Planning Zoning & Design Board",
    "planning zoning & design board": "Planning Zoning & Design Board",
    "planning, zoning & design board": "Planning Zoning & Design Board",
    "planning zoning and design board": "Planning Zoning & Design Board",
    "special called meeting": "Special Meeting",
}


@dataclass(frozen=True)
class AgendaEntry:
    title: str | None
    action_text: str
    vote_text: str | None = None


def normalize_meeting_type(value: str | None) -> str | None:
    if not value:
        return None
    key = re.sub(r"\s+", " ", value.strip().lower())
    return MEETING_TYPE_ALIASES.get(key, value.strip())


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    # Strip commas so "February 3, 2021" and "February 3 2021" both parse —
    # regex callers re-join capture groups without the comma.
    value = value.strip().replace(",", " ")
    value = re.sub(r"\s+", " ", value)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value.title(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _valid_meeting_date(year: int, month: int, day: int) -> str | None:
    if not (MEETING_YEAR_MIN <= year <= MEETING_YEAR_MAX):
        return None
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def _date_from_text(text: str) -> str | None:
    for match in re.finditer(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", text or ""):
        parsed = parse_date(f"{match.group(1)} {match.group(2)} {match.group(3)}")
        if parsed and MEETING_YEAR_MIN <= int(parsed[:4]) <= MEETING_YEAR_MAX:
            return parsed
    return None


# Filename date patterns, ordered most→least explicit. Each yields
# (year, month, day) group indices.
_FILENAME_YMD_PATTERNS = (
    r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)",       # 2024-03-04
    r"(?<!\d)(20\d{2})(\d{2})-(\d{2})(?!\d)",      # 202403-04
    r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)",       # 20240304
)
_FILENAME_MDY_PATTERNS = (
    r"(?<!\d)(\d{1,2})(\d{2})(20\d{2})(?!\d)",     # 03042024 / 3042024
    r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)",         # 030424
)


def extract_date(filename: str, text: str) -> tuple[str | None, str]:
    # Filename first: meeting filenames encode the meeting's own date, while
    # PDF text often opens with boilerplate that mentions unrelated dates
    # ("Video recordings ... from June 8, 2016 forward"). A pattern that
    # matches but yields an impossible date (e.g. month 0 from a typo like
    # "0232021") falls through to the next pattern instead of giving up.
    for pattern in _FILENAME_YMD_PATTERNS:
        match = re.search(pattern, filename)
        if match:
            year, month, day = (int(g) for g in match.groups())
            parsed = _valid_meeting_date(year, month, day)
            if parsed:
                return parsed, "filename"
    for pattern in _FILENAME_MDY_PATTERNS:
        match = re.search(pattern, filename)
        if match:
            month, day, year = match.groups()
            year = f"20{year}" if len(year) == 2 else year
            parsed = _valid_meeting_date(int(year), int(month), int(day))
            if parsed:
                return parsed, "filename"

    parsed = _date_from_text(text)
    if parsed:
        return parsed, "pdf_text"

    return None, "missing"


def extract_start_time(text: str) -> str | None:
    patterns = [
        r"(?:Call to Order|Started|Order)(?:\s+at)?[:\s]+(\d{1,2}[:.]\d{2}\s*[ap]\.?m\.?)",
        r"\b(\d{1,2}[:.]\d{2}\s*[ap]\.?m\.?)\b",
    ]
    return _first_time(text, patterns)


def extract_end_time(text: str) -> str | None:
    patterns = [
        r"(?:Adjourned|Adjournment|Time Adjourned|Ended)(?:\s+at)?[:\s]+(\d{1,2}[:.]\d{2}\s*[ap]\.?m\.?)",
        r"(\d{1,2}[:.]\d{2}\s*[ap]\.?m\.?)\s+(?:Adjourned|Adjournment)",
    ]
    return _first_time(text, patterns)


def _first_time(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).replace(".", "").lower().strip()
    return None


def extract_staff_code(text: str) -> str | None:
    match = re.search(r"\b([A-Za-z]{2}/[A-Za-z]{2})\b", text)
    return match.group(1).upper() if match else None


CANCELLATION_NOTICE_PATTERN = re.compile(
    r"\b(?:"
    r"notice\s+of\s+cancellation"
    r"|cancelled\s+meeting\s+notice"
    r"|cancellation\s+of\s+(?:the\s+)?(?:scheduled\s+)?meeting"
    r"|this\s+(?:meeting|hearing|workshop)\s+(?:has\s+been\s+|is\s+|was\s+)?cancelled"
    r")\b",
    re.I,
)


def infer_meeting_type(filename: str, text: str, fallback: str | None = None) -> str:
    normalized_fallback = normalize_meeting_type(fallback) if fallback else None
    filename_blob = filename.lower()
    # Only the first ~600 chars: a real cancellation notice puts the phrase in
    # the header, not buried in body text that references an unrelated
    # cancelled item.
    if "cancel" in filename_blob or CANCELLATION_NOTICE_PATTERN.search((text or "")[:600]):
        return "Cancelled Meeting"
    blob = f"{filename} {text[:1200]}".lower()
    if normalized_fallback and (
        "pzdb" in filename_blob
        or "planning zoning" in filename_blob
        or "planning, zoning" in filename_blob
        or "village council" in normalized_fallback.lower()
        or "council" in normalized_fallback.lower()
    ):
        return normalized_fallback
    if "joint workshop" in blob:
        return "Joint Workshop"
    if "zoning hearing and comp plan workshop" in blob or "zoning hearing and comprehensive plan workshop" in blob:
        return "Combined Zoning Hearing / Workshop"
    if "organizational business meeting" in blob or "organizational meeting" in blob:
        return "Organizational Meeting"
    if "special emergency meeting" in blob:
        return "Special Emergency Meeting"
    if "special meeting budget hearing" in blob or "budget hearing" in blob or "millage" in blob:
        return "Budget Hearing"
    if "council special meeting" in blob or "special meeting" in blob:
        return "Special Meeting"
    if "comp plan workshop" in blob or "comprehensive plan workshop" in blob:
        return "Comprehensive Plan Workshop"
    if "planning zoning" in blob or "p&z" in blob or "pzdb" in blob:
        return "Planning Zoning & Design Board"
    if "public information" in blob or "open house" in blob:
        return "Public Information Meeting"
    if "zoning and dri development order hearing" in blob or "zoning hearing" in blob:
        return "Zoning Hearing"
    if "public hearing" in blob:
        return "Public Hearing"
    if "workshop" in blob:
        return "Workshop"
    if normalized_fallback:
        return normalized_fallback
    return "Village Council Regular Meeting"


def extract_agenda_entries(text: str) -> list[AgendaEntry]:
    if not text:
        return []

    entries: list[AgendaEntry] = []
    entries.extend(_extract_action_marker_entries(text))
    entries.extend(_extract_agenda_section_entries(text))
    entries.extend(_extract_narrative_motion_entries(text))

    if entries:
        return _dedupe_entries(entries)

    fallback_patterns = [
        r"\b(Approved(?!\s+BY\s+(?:BOARD|COUNCIL))\s+.*?)(?=\s+(?:Motion:|Vote:|Action:|Public Comment|Adjourned|$))",
        r"\b(Adopted\s+.*?)(?=\s+(?:Motion:|Vote:|Action:|Public Comment|Adjourned|$))",
        r"\b(Passed\s+.*?)(?=\s+(?:Motion:|Vote:|Action:|Public Comment|Adjourned|$))",
    ]
    actions: list[str] = []
    for pattern in fallback_patterns:
        actions.extend(_clean_action(s) for s in re.findall(pattern, text, flags=re.I))
    return _dedupe_entries([AgendaEntry(title=None, action_text=a) for a in actions if len(a) > 12])


def _extract_action_marker_entries(text: str) -> list[AgendaEntry]:
    entries: list[AgendaEntry] = []
    # The numbered-heading stop (`12. NEW BUSINESS`) must not fire on the tail
    # of a hyphenated ordinance number ("Ordinance No. 2025-16. Vote:"), so the
    # digits may not be preceded by a digit or hyphen.
    for match in re.finditer(
        r"\bAction:\s*(.*?)(?=\s*(?:Vote:|Motion:|Action:|Staff Presentation|Council Questions|Public Comment|Public Input|Board Communications|Adjourned|Adjournment|(?:(?<![\d-])\d{1,2}\.|[A-Z]\)|\([a-z0-9]\))\s+[A-Z]|$))",
        text,
        flags=re.I,
    ):
        action = _clean_action(match.group(1))
        if len(action) <= 8:
            continue
        vote_text = _extract_following_vote_text(text[match.end():])
        title = _infer_agenda_title(text[max(0, match.start() - 5000):match.start()])
        entries.append(AgendaEntry(title=title, action_text=action, vote_text=vote_text))
    return entries


def extract_actions(text: str) -> list[str]:
    return [entry.action_text for entry in extract_agenda_entries(text)]


def split_csv_actions(action_taken: str | None) -> list[str]:
    if not action_taken:
        return []
    if action_taken in {"No action found", "No action extracted - verify PDF"}:
        return []
    return _dedupe([_clean_action(s) for s in action_taken.split("|") if len(_clean_action(s)) > 8])


def _clean_action(text: str) -> str:
    text = re.sub(
        r"\b(?:Village Council|Planning Zoning(?: & Design Board)?|Council Workshop|Special Meeting)"
        r".{0,80}?\s+Page\s+\d+\s+of\s+\d+\b",
        " ",
        text,
        flags=re.I,
    )
    text = re.sub(r"Vote\s*:\s*(?:\(.*?\))?\s*Aye\s*:?", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" .;:-")
    return text


def _extract_following_vote_text(text: str) -> str | None:
    match = re.match(
        r"\s*Vote:\s*(.*?)(?=\s*(?:Motion:|Action:|Staff Presentation|Council Questions|Public Comment|Public Input|Board Communications|Adjourned|Adjournment|(?:(?<![\d-])\d{1,2}\.|[A-Z]\)|\([a-z0-9]\))\s+[A-Z]|$))",
        text,
        flags=re.I | re.S,
    )
    if not match:
        return None
    value = re.sub(r"\s+", " ", match.group(1)).strip(" .;:")
    return value or None


SECTION_HEADER_PATTERN = re.compile(
    r"(?<![A-Za-z])(?:PUBLIC INFORMATION(?:\s+MEETINGS?)?|PUBLIC HEARINGS?|"
    r"WORKSHOPS?(?:\s+ITEMS?)?|"
    r"BUSINESS(?:\s+ITEMS?)?|ACTION ITEMS?|UNFINISHED BUSINESS|NEW BUSINESS|"
    r"FIRST READING(?:\s+OF\s+ORDINANCES?)?|SECOND READING(?:\s+OF\s+ORDINANCES?)?|"
    r"CONSENT AGENDA|COUNCIL BUSINESS|COUNCIL ACTION|"
    r"ORDINANCES?\s*[-–]\s*(?:FIRST|SECOND)\s+READING|"
    r"RESOLUTIONS?(?=\s+(?:\([a-z]\)|\(\d+\)|\d+\(|No\.))|"
    r"BOARD ACTION|RECOMMENDATIONS?|PRESENTATIONS?(?=\s+(?:\([a-z]\)|\(\d+\)|\d+\())|"
    r"OLD BUSINESS|REGULAR BUSINESS|"
    r"BOARD COMMUNICATIONS|COUNCIL COMMUNICATIONS|"
    r"COUNCIL\s*[/I]?\s*MANAGER\s*[/I]?\s*ATTORNEY\s+COMMUNICATIONS)"
    r"(?:\s*[:–—-])?\s+"
    r"(?=\([a-z]\)|\(\d{1,2}\)|\d{1,2}\([A-Z@]\)|[A-Z]\)\s+|\([A-Z][a-z])",
    flags=re.I,
)

SECTION_STOP_PATTERN = re.compile(
    r"\s+(?:"
    r"PUBLIC INPUT(?:\s+ON\s+NON[\s-]?AGENDA)?\b"
    r"|BOARD COMMUNICATIONS\b"
    r"|COUNCIL\s*[/I]?\s*(?:MANAGER\s*[/I]?\s*ATTORNEY\s*)?COMMUNICATIONS(?:\s+(?:AND|I|/)\s+FUTURE\s+AGENDA)?\b"
    r"|VILLAGE MANAGER(?:['’]?S)?\s+(?:REPORT|COMMENTS)\b"
    r"|VILLAGE ATTORNEY(?:['’]?S)?\s+(?:REPORT|COMMENTS)\b"
    r"|VILLAGE CLERK(?:['’]?S)?\s+(?:REPORT|COMMENTS)\b"
    r"|MAYOR(?:['’]?S)?\s+(?:REPORT|COMMENTS)\b"
    r"|ADJOURNMENT\b|ADJOURN\s+(?:at|the|Regular)"
    r"|FUTURE AGENDA ITEMS\b"
    r"|(?-i:\d{1,2}\.\s+[A-Z][A-Z ]{3,})"
    r")(?=\s+|\(|$)",
    flags=re.I,
)

SUB_ITEM_PATTERN = re.compile(
    r"(?:^|\s)(\([a-zA-Z@]\)|\(\d{1,2}\)|\d{1,2}\([A-Z@]\))\s+(?=[A-Z0-9\"'])",
)

INTERNAL_ACTION_PATTERN = re.compile(
    r"(?<!Final\s)(?<!final\s)\b(?:Action|Recommendation)\s*:\s*(.*?)(?=\s*(?:Vote:|Motion:|Public Comment|Public Input|Board Communications|Adjourn|$))",
    flags=re.I | re.S,
)

NARRATIVE_MOTION_PATTERN = re.compile(
    r"(?:Mr\.?|Ms\.?|Mrs\.?|Mayor|Vice[\s-]?Mayor|Councilmember|Council\s*Member|"
    r"Vice\s+Chairman|Chair(?:man|person)|Board\s+Member|Interim\s+Village\s+\w+)\s+"
    r"[A-Z][A-Za-z'-]+\s+"
    r"(?:moved|moves|move)\s+(?:approval|adoption|to\s+\w+|that|the|nomination).*?"
    r"(?:called\s+and\s+carried|carried\s+unanimously|carried\s+\d+\s*[-/to]\s*\d+|"
    r"carried\s+with\s+[\w\s,'.-]+?(?:dissenting|absent|opposed|abstaining)|"
    r"unanimously|motion\s+(?:failed|passed|carried))",
    flags=re.I | re.S,
)

# Passive PZDB phrasing: "A motion to approve the consent agenda was made and
# duly passed." — no mover is named, so NARRATIVE_MOTION_PATTERN never fires.
PASSIVE_MOTION_PATTERN = re.compile(
    r"(?:A|The)\s+motion\s+(?:to|for|that)\b[^.]{0,200}?\bwas\s+made\b[^.]{0,200}?"
    r"\b(?:passed|carried|approved|adopted|denied|failed)\b",
    flags=re.I,
)


PAGE_HEADER_PATTERN = re.compile(
    r"\s+(?:Village Council|Planning Zoning(?:\s+(?:and|&)\s+Design\s+Board)?|"
    r"Council Workshop|Special Meeting)"
    r"(?:\s+Minutes)?\s*[–—-]\s*[A-Z][a-z]+\s+\d{1,2},?\s+\d{4}\s+Page\s+\d+\s+of\s+\d+",
    flags=re.I,
)


def _strip_page_headers(text: str) -> str:
    return PAGE_HEADER_PATTERN.sub(" ", text)


def _extract_agenda_section_entries(text: str) -> list[AgendaEntry]:
    normalized = re.sub(r"\s+", " ", text)
    normalized = _strip_page_headers(normalized)
    entries: list[AgendaEntry] = []

    section_starts = _find_section_starts(normalized)
    for sec_idx, (sec_start, sec_header_end) in enumerate(section_starts):
        next_section_start = (
            section_starts[sec_idx + 1][0]
            if sec_idx + 1 < len(section_starts)
            else len(normalized)
        )
        stop = SECTION_STOP_PATTERN.search(normalized, sec_header_end, next_section_start)
        section_end = stop.start() if stop else next_section_start
        section_body = normalized[sec_header_end:section_end].strip(" .;:")
        if len(section_body) < 30:
            continue

        sub_markers = list(SUB_ITEM_PATTERN.finditer(section_body))
        if not sub_markers:
            if _section_has_project_signal(section_body):
                title = _clean_title(section_body)
                if title and not _is_noise_title(title):
                    entries.append(AgendaEntry(
                        title=title,
                        action_text=f"No formal action recorded. {section_body[:900]}",
                    ))
            continue

        for i, marker in enumerate(sub_markers):
            sub_start = marker.end()
            sub_end = (
                sub_markers[i + 1].start()
                if i + 1 < len(sub_markers)
                else len(section_body)
            )
            sub_text = section_body[sub_start:sub_end].strip(" .;:")
            if len(sub_text) < 20:
                continue
            title = _clean_title(_title_prefix(sub_text))
            if not title or _is_noise_title(title):
                continue

            action_text = _extract_sub_item_action(sub_text)
            vote_text = _extract_following_vote_text(sub_text[_action_end(sub_text):]) if action_text else None
            if action_text:
                entries.append(AgendaEntry(title=title, action_text=action_text, vote_text=vote_text))
            elif len(title) >= 12:
                entries.append(AgendaEntry(
                    title=title,
                    action_text=f"No formal action recorded. {sub_text[:900]}",
                ))
    return entries


def _find_section_starts(normalized: str) -> list[tuple[int, int]]:
    results: list[tuple[int, int]] = []
    for match in SECTION_HEADER_PATTERN.finditer(normalized):
        results.append((match.start(), match.end()))
    return results


def _title_prefix(sub_text: str) -> str:
    cutoff = len(sub_text)
    for pattern in (
        r"\bStaff Presentation",
        r"\bStaff Comments",
        r"\bPresentation/Information",
        r"\bCouncil Questions",
        r"\bBoard Questions",
        r"\bPublic Comment",
        r"\bPublic Input",
        r"\bMotion\s*:",
        r"\bAction\s*:",
        r"\bRecommendation\s*:",
        r"\bAdjourn",
        # Narrative cues that signal the end of an item heading and the start
        # of discussion content. Without these, secondary mentions in the body
        # (e.g. "Discussion ensued regarding Koreshan State Park") leak into
        # the title and trigger spurious location matches.
        r"\bDiscussion ensued",
        r"\bDiscussion followed",
        r"\bFollowing discussion",
        r"\bBrief discussion",
        r"\bCouncil discussion",
        r"\bBoard discussion",
        r"\bCouncil direction",
        r"\bDirection to staff",
        r"\bA motion (?:to|was|for|that)\b",
        r"\bReferred to (?:his|her|their|the) (?:memorandum|memo|memo[a-z]+)",
        r"\bReferred to staff",
        r"\bConsensus was",
        r"\b[A-Z][a-z]+ \w*\s*moved\b",
        r"\bMayor [A-Z][a-z]+ (?:asked|called|noted|stated|explained|opened|closed)\b",
        r"\bVice[\s-]?Mayor [A-Z][a-z]+ (?:asked|called|noted|stated|explained)\b",
        r"\bVillage (?:Manager|Attorney|Clerk) [A-Z][a-z]+ (?:asked|called|noted|stated|explained|provided|reported)\b",
        r"\bVice Chair(?:man|person)? [A-Z][a-z]+ (?:asked|called|noted|stated|explained)\b",
        r"\bChair(?:man|person)? [A-Z][a-z]+ (?:asked|called|noted|stated|explained|opened|closed)\b",
        r"\bBoard Member [A-Z][a-z]+ (?:asked|called|noted|stated|explained|recused)\b",
        r"\bCouncilmember [A-Z][a-z]+ (?:asked|called|noted|stated|explained|recused)\b",
    ):
        m = re.search(pattern, sub_text, flags=re.I)
        if m and m.start() < cutoff:
            cutoff = m.start()
    return sub_text[:cutoff].strip(" .;:-")


def _extract_sub_item_action(sub_text: str) -> str | None:
    m = INTERNAL_ACTION_PATTERN.search(sub_text)
    if m:
        action = _clean_action(m.group(1))
        if len(action) > 8:
            return action
    m = NARRATIVE_MOTION_PATTERN.search(sub_text)
    if m:
        action = _clean_action(m.group(0))
        if len(action) > 8:
            return action
    m = PASSIVE_MOTION_PATTERN.search(sub_text)
    if m:
        action = _clean_action(m.group(0))
        if len(action) > 8:
            return action
    if re.search(r"\bDeferred(?:\s+to)?\b", sub_text, flags=re.I):
        return _clean_action(re.search(r"\bDeferred[^.]{0,200}", sub_text, flags=re.I).group(0))
    if re.search(r"\bContinued(?:\s+to)?\b", sub_text, flags=re.I):
        return _clean_action(re.search(r"\bContinued[^.]{0,200}", sub_text, flags=re.I).group(0))
    return None


def _action_end(sub_text: str) -> int:
    m = INTERNAL_ACTION_PATTERN.search(sub_text)
    if m:
        return m.end()
    m = NARRATIVE_MOTION_PATTERN.search(sub_text)
    if m:
        return m.end()
    m = PASSIVE_MOTION_PATTERN.search(sub_text)
    if m:
        return m.end()
    return 0


def _extract_narrative_motion_entries(text: str) -> list[AgendaEntry]:
    """Catch 2015-2017 style narrative items keyed off motion+carry phrases.

    These minutes lack explicit Action: markers. Strategy: find each
    "[Member] moved ... called and carried" phrase, then look backward in a
    bounded window for the most recent item marker — a parenthesised letter
    like (A), a numbered/lettered marker like 5(A), an OCR variant like S(A),
    or a RESOLUTION/ORDINANCE/Reading keyword — to use as the title context.
    """
    normalized = re.sub(r"\s+", " ", text)
    entries: list[AgendaEntry] = []

    motion_carry_pattern = re.compile(
        r"((?:Mr\.?|Ms\.?|Mrs\.?|Mayor|Vice[\s-]?Mayor|Councilmember|Council\s*Member|"
        r"Vice\s+Chairman|Chair(?:man|person)|Board\s+Member|Interim\s+Village\s+\w+)\s+"
        r"[A-Z][A-Za-z'-]+\s+"
        r"(?:moved|moves|move)\s+(?:approval|adoption|to\s+\w+|that|the|nomination).*?"
        r"(?:called\s+and\s+carried|carried\s+unanimously|carried\s+\d+\s*[-/to]\s*\d+|"
        r"carried\s+with\s+[\w\s,'.-]+?(?:dissenting|absent|opposed|abstaining)|"
        r"motion\s+(?:failed|passed|carried)))",
        flags=re.I | re.S,
    )

    item_marker_pattern = re.compile(
        r"(?:(?<=\s)|(?<=^)|(?<=\d)|(?<=[Ss]))\(([A-Za-z@]|\d{1,2})\)\s+",
    )

    title_keyword_pattern = re.compile(
        r"\b(RESOLUTION|ORDINANCE|FIRST\s+READING|SECOND\s+READING|"
        r"PUBLIC\s+HEARING|DEVELOPMENT\s+ORDER|REZONING|"
        r"APPROVAL\s+OF\s+\w+|CONSENT\s+AGENDA)\b[^.]{0,300}",
        flags=re.I,
    )

    last_anchor = 0
    for m in motion_carry_pattern.finditer(normalized):
        action = _clean_action(m.group(1))
        if len(action) < 20:
            continue
        # Title lookup window: from previous anchor up to motion start
        window = normalized[last_anchor:m.start()]
        title = _title_from_window(window, item_marker_pattern, title_keyword_pattern)
        if not title or _is_noise_title(title):
            last_anchor = m.end()
            continue
        entries.append(AgendaEntry(title=title, action_text=action))
        last_anchor = m.end()

    return entries


def _title_from_window(
    window: str,
    item_marker_pattern: re.Pattern[str],
    title_keyword_pattern: re.Pattern[str],
) -> str | None:
    markers = list(item_marker_pattern.finditer(window))
    if markers:
        last = markers[-1]
        candidate = window[last.end():]
    else:
        keywords = list(title_keyword_pattern.finditer(window))
        if not keywords:
            return None
        candidate = window[keywords[-1].start():]
    candidate = re.sub(r"\s+", " ", candidate).strip(" .;:-")
    candidate = re.split(r"(?:Mr\.?|Ms\.?|Mrs\.?|Mayor|Vice[\s-]?Mayor|Councilmember|Vice\s+Chairman|Chair(?:man|person)|Board\s+Member)\s+[A-Z]", candidate, maxsplit=1, flags=re.I)[0]
    return _clean_title(candidate[:260])


def _section_has_project_signal(section: str) -> bool:
    return bool(
        re.search(r"\b(?:DOS|LDO|DCI|COP|ADD|CPA|ZTA|DO)\s*\d{4}-[A-Z]?\d{3}\b", section, flags=re.I)
        or re.search(r"\b\d{3,6}\s+[A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,5}\s+(?:Road|Rd|Street|St|Avenue|Ave|Parkway|Pkwy|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Circle|Cir|Way|Terrace|Place|Pl)\b", section, flags=re.I)
        or re.search(r"\b(?:Resolution|Ordinance)\s+No\.\s*\d{4}-\d+\b", section, flags=re.I)
    )


def _infer_agenda_title(context: str) -> str | None:
    context = re.sub(
        r"\b(?:Village Council|Planning Zoning(?: & Design Board)?|Council Workshop|Special Meeting)"
        r".{0,80}?\s+Page\s+\d+\s+of\s+\d+\b",
        " ",
        context,
        flags=re.I,
    )
    context = re.sub(r"\s+", " ", context).strip()
    if not context:
        return None
    last_motion = context.lower().rfind(" motion:")
    if last_motion >= 0:
        context = context[:last_motion].strip()

    marker_pattern = re.compile(
        r"(?:^|\s)(?:(?<!US\s)(?<!U\.S\.\s)(?:[1-9]|1\d|20)\.|[A-Z]\)|\([a-z]\)|\(\d{1,2}\))\s+",
        flags=re.I,
    )
    stop_pattern = re.compile(
        r"\s+(?:Motion:|Staff Presentation|Staff Comments|Council Questions|Council Comments|"
        r"Public Comment|Village Clerk|Village Manager|Questions or Comments|Action:|Vote:)",
        flags=re.I,
    )
    markers = list(marker_pattern.finditer(context))
    candidates: list[str] = []
    for index, match in enumerate(markers):
        start = match.end()
        next_marker_start = markers[index + 1].start() if index + 1 < len(markers) else len(context)
        end = next_marker_start
        stop = stop_pattern.search(context, start, next_marker_start)
        if stop:
            end = stop.start()
        title = _clean_title(context[start:end])
        if title and not _is_noise_title(title):
            candidates.append(title)
    return candidates[-1] if candidates else None


def _clean_title(text: str) -> str | None:
    text = re.sub(r"\s+", " ", text).strip(" .;:-")
    text = re.sub(r"^(?:and\s+)?", "", text, flags=re.I).strip()
    if not text:
        return None
    return text[:255]


def _is_noise_title(title: str) -> bool:
    lo = title.lower().strip()
    if lo in {"aye", "nay", "abstentions", "none"} or lo.startswith("final action agenda"):
        return True
    if re.match(r"next\s+meeting\b", lo):
        return True
    if lo.startswith("conflict of interest"):
        return True
    if re.match(r"review\s+\d{4}\s+(?:planning|village|council|board)", lo):
        return True
    return False


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.lower()
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _dedupe_entries(values: list[AgendaEntry]) -> list[AgendaEntry]:
    out: list[AgendaEntry] = []
    for value in values:
        merged = False
        for index, existing in enumerate(out):
            if _entries_are_duplicates(existing, value):
                out[index] = _merge_entries(existing, value)
                merged = True
                break
        if not merged:
            out.append(value)
    return out


def _dedupe_text_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _entries_are_duplicates(a: AgendaEntry, b: AgendaEntry) -> bool:
    """True when two entries describe the same agenda item.

    The action-marker and section extractors often both capture an item, with
    action texts that differ only in where the stop-lookahead cut them — so
    equal actions always match, and a prefix relationship matches when the
    titles (if both present) agree.
    """
    action_a = _dedupe_text_key(a.action_text)
    action_b = _dedupe_text_key(b.action_text)
    if not action_a or not action_b:
        return False
    if action_a == action_b:
        return True
    shorter, longer = sorted((action_a, action_b), key=len)
    if len(shorter) < 12 or not longer.startswith(shorter):
        return False
    title_a = _dedupe_text_key(a.title)
    title_b = _dedupe_text_key(b.title)
    if title_a and title_b:
        title_short, title_long = sorted((title_a, title_b), key=len)
        return title_long.startswith(title_short)
    return True


def _merge_entries(a: AgendaEntry, b: AgendaEntry) -> AgendaEntry:
    primary = a if len(a.action_text) >= len(b.action_text) else b
    secondary = b if primary is a else a
    return AgendaEntry(
        title=primary.title or secondary.title,
        action_text=primary.action_text,
        vote_text=primary.vote_text or secondary.vote_text,
    )


def raw_pdf_url(filename: str, repo: str = "EagleGIS-FGCU/EagleGIS", branch: str = "script") -> str:
    escaped = Path(filename).name.replace(" ", "%20")
    return f"https://raw.githubusercontent.com/{repo}/{branch}/pdfs/{escaped}"

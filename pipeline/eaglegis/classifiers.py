from __future__ import annotations

import re

from .config import CATEGORY_DEFINITIONS, CATEGORY_TERMS, LOCATION_SEEDS, PROJECT_ALIASES


VOTE_TERMS = [
    "motion", "seconded", "vote", "roll call", "aye", "nay",
    "approved", "adopted", "passed", "authorized", "continued",
]


def vote_detected(text: str) -> bool:
    lo = text.lower()
    return any(term in lo for term in VOTE_TERMS)


CANCELLED_ACTION_PATTERN = re.compile(
    r"\b(?:"
    r"notice\s+of\s+cancellation"
    r"|cancelled\s+meeting\s+notice"
    r"|this\s+(?:meeting|hearing|workshop)\s+(?:has\s+been\s+|is\s+|was\s+)?cancelled"
    r"|meeting\s+(?:has\s+been\s+|is\s+|was\s+)cancelled"
    r")\b",
    re.I,
)


def infer_action_type(text: str, meeting_type: str) -> str:
    lo = text.lower()
    if CANCELLED_ACTION_PATTERN.search(text):
        return "No Action"
    if any(term in lo for term in [
        "approve agenda", "approved agenda", "agenda as amended",
        "remote participation", "participate remotely", "excused",
    ]):
        return "Administrative"
    if "consent agenda" in lo:
        return "Consent Agenda"
    if "ordinance" in lo or "first reading" in lo or "second reading" in lo:
        return "Ordinance"
    if "resolution" in lo:
        return "Resolution"
    if any(term in lo for term in ["contract", "task authorization", "change order", "agreement"]):
        return "Contract Approval"
    if any(term in lo for term in ["budget", "millage", "capital improvement"]):
        return "Budget"
    if "public comment" in lo:
        return "Public Comment"
    if "presentation" in lo:
        return "Presentation"
    if "workshop" in meeting_type.lower() or any(term in lo for term in ["discussion", "consensus", "direction"]):
        return "Discussion"
    if vote_detected(text):
        return "Vote"
    return "Unknown"


SUBJECT_CATEGORIES = {
    "Residential Development",
    "Commercial & Mixed-Use Development",
    "Industry, Mining & Agriculture",
    "Transportation & Mobility",
    "Utilities, Stormwater & Environment",
    "Public Facilities & Services",
}


def infer_category(text: str, action_type: str) -> str:
    lo = text.lower()
    scores: dict[str, int] = {}
    for definition in CATEGORY_DEFINITIONS:
        category = str(definition["name"])
        score = 0
        for term in CATEGORY_TERMS[category]:
            if term in lo:
                score += 3 if " " in term else 1
        if score:
            scores[category] = score

    ordered_categories = [str(definition["name"]) for definition in CATEGORY_DEFINITIONS]
    # A contract/budget/ordinance item about a road, utility, facility, or
    # development should map to the underlying subject — not collapse into the
    # procedural "Budget, Contracts & Purchasing" bucket just because that
    # category's terms (contract, grant, reimbursement) also matched.
    subject_scores = {c: s for c, s in scores.items() if c in SUBJECT_CATEGORIES}
    if subject_scores:
        return max(
            subject_scores,
            key=lambda category: (subject_scores[category], -ordered_categories.index(category)),
        )
    if scores:
        return max(
            scores,
            key=lambda category: (scores[category], -ordered_categories.index(category)),
        )

    if action_type in {"Administrative", "Consent Agenda", "Discussion", "Presentation", "Public Comment", "No Action"}:
        return "Meetings, Records & Public Input"
    if action_type in {"Contract Approval", "Budget"}:
        return "Budget, Contracts & Purchasing"
    return "Meetings, Records & Public Input"


def match_projects(text: str, fallback_project: str | None = None) -> list[str]:
    lo = text.lower()
    matches = [
        project for project, aliases in PROJECT_ALIASES.items()
        if any(re.search(rf"\b{re.escape(alias)}\b", lo) for alias in aliases)
    ]
    if fallback_project and fallback_project not in matches:
        matches.append(fallback_project)
    return matches


def match_locations(text: str, fallback_location: str | None = None) -> list[str]:
    lo = text.lower()
    matches = []
    for name, data in LOCATION_SEEDS.items():
        aliases = [name, *(data.get("aliases") or [])]
        if any(_location_alias_matches(lo, alias) for alias in aliases):
            matches.append(name)
    if fallback_location and fallback_location not in matches:
        matches.append(fallback_location)
    return matches


# Aliases that frequently match in passing — when the surrounding word changes
# the meaning (e.g. "Coconut Point" the mall vs "Via Coconut Point" the street),
# suppress the match so the wrong seed isn't pinned to the agenda item.
ALIAS_NEGATIVE_CONTEXT = {
    "coconut point": [r"\bvia\s+coconut\s+point\b"],
    "coconut road": [r"\bvia\s+coconut\b"],
    "sandy lake": [r"\bsandy\s+lake\s+(?:drive|dr|road|rd|boulevard|blvd|lane|ln|circle|cir|estates?)\b"],
    "estero parkway": [
        r"\bestero\s+parkway\s+(?:to|from)\b",
        r"\b(?:to|from|near|along|past|south\s+of|north\s+of)\s+estero\s+parkway\b",
    ],
    "estero river": [
        r"\bestero\s+river\s+(?:road|circle|court|drive|lane|way|trail|cir|ct|dr|ln|rd)\b",
    ],
    "tamiami trail": [
        r"\b(?:to|from|along|near|across)\s+(?:the\s+)?tamiami\s+trail\b",
    ],
    "us 41": [
        r"\b(?:to|from|along|near|across|east\s+of|west\s+of|north\s+of|south\s+of)\s+us\s+41\b",
    ],
    "u.s. 41": [
        r"\b(?:to|from|along|near|across|east\s+of|west\s+of|north\s+of|south\s+of)\s+u\.s\.\s+41\b",
    ],
}


def _location_alias_matches(text: str, alias: str) -> bool:
    alias = alias.lower()
    if alias == "bert" and re.search(r"\bbert\s+harris\b", text):
        return False
    if not re.search(rf"\b{re.escape(alias)}\b", text):
        return False
    suppressors = ALIAS_NEGATIVE_CONTEXT.get(alias)
    if not suppressors:
        return True
    # If every occurrence of the alias sits inside a suppressing context, drop it.
    occurrences = list(re.finditer(rf"\b{re.escape(alias)}\b", text))
    for occ in occurrences:
        start, end = occ.start(), occ.end()
        # Look in a 30-char window on each side for the suppressing context.
        window_start = max(0, start - 30)
        window_end = min(len(text), end + 30)
        window = text[window_start:window_end]
        if not any(re.search(p, window) for p in suppressors):
            return True
    return False


def extract_address_candidates(text: str) -> list[str]:
    """Find address-like references suitable for geocoding review."""
    suffixes = (
        "Road", "Rd", "Street", "St", "Avenue", "Ave", "Parkway", "Pkwy",
        "Boulevard", "Blvd", "Lane", "Ln", "Drive", "Dr", "Court", "Ct",
        "Circle", "Cir", "Way", "Terrace", "Place", "Pl", "Trail", "Trl",
        "Highway", "Hwy",
    )
    suffix_pattern = "|".join(suffixes)
    token = r"(?:[A-Z][A-Za-z.'-]*|[A-Z]{1,4}|[0-9]+(?:st|nd|rd|th)?)"
    via_token = r"(?:(?!Estero|Florida|Suite|Building|East|West|North|South|FL)[A-Z][A-Za-z.'-]*)"
    patterns = [
        rf"\b\d{{3,6}}\s+(?:{token}\s+){{0,5}}(?:{suffix_pattern})\b",
        rf"\b\d{{3,5}}\s+Block\s+(?:{token}\s+){{0,5}}(?:{suffix_pattern})\b",
        # Italian-style street names common in Estero developments
        # (Via Villagio, Via Coconut Point, Strada Nuova Circle, etc.)
        rf"\b\d{{3,6}}\s+Via\s+(?:{via_token}\s+){{0,3}}{via_token}\b",
        rf"\b\d{{3,6}}\s+Strada\s+(?:{via_token}\s+){{0,3}}{via_token}\b",
        rf"\b\d{{3,6}}\s+Plaza\s+(?:{via_token}\s+){{0,3}}{via_token}\b",
    ]
    candidates: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            value = re.sub(r"\s+", " ", match.group(0)).strip(" .,;")
            if not _looks_like_address(value):
                continue
            if value.lower() not in {c.lower() for c in candidates}:
                candidates.append(value)
    return candidates


def _looks_like_address(value: str) -> bool:
    lo = value.lower()
    bad_fragments = [
        "contract", "engineering", "services", "construction", "budget",
        "workshop", "minutes", "agenda", "meeting", "action", "amendment",
        "acceptance", "provide", "proposed", "replace", "repair",
    ]
    if any(fragment in lo for fragment in bad_fragments):
        return False
    first = re.match(r"\d+", value)
    if first and int(first.group(0)) == 0:
        return False
    return True


def needs_review(
    *,
    needs_ocr: bool,
    date_missing: bool,
    action_count: int,
    project_count: int,
    location_count: int,
    used_csv_fallback: bool,
) -> bool:
    return (
        needs_ocr
        or date_missing
        or action_count == 0
        or project_count == 0
        or location_count == 0
        or used_csv_fallback
    )

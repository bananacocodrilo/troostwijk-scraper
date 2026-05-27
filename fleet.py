"""Fleet-type detection from listing free-text.

Scans title + remarks + additional_information for signals about a van's
previous service. The category is informational (surfaced as a dashboard
badge); it does not currently affect the suitability score.

Categories — ordered by detection priority:
  - utility       grid / municipal / waterworks fleet  (good signal)
  - telecom       KPN / Ziggo / fiber installer fleet
  - delivery      PostNL / DHL / DPD / UPS courier      (bad signal)
  - solar         solar panel installer
  - refrigeration refrigerated transport (also usually hard-rejected upstream)
  - workshop      fitted service van                    (also usually hard-rejected upstream)
  - construction  contractor / builder fleet
  - hire          ex-rental / ex-lease
  - private       no fleet signal — likely private/lease return
"""

import re
from typing import List, Optional, Tuple

# (label, regex patterns, human description)
FLEET_PATTERNS: List[Tuple[str, List[str], str]] = [
    ("utility", [
        r"\bstedin\b", r"\bliander\b", r"\benexis\b",
        r"\bwaterschap\b", r"\bgemeente\b", r"\bmunicipal",
        r"\brijkswaterstaat\b", r"\bnetbeheer", r"\bnetwerkbeheer",
        r"\bgrid\s*operator", r"\bcouncil\s*fleet",
        r"\bstadswerk", r"\bstadsdienst",
    ], "utility / municipal / grid fleet"),

    ("telecom", [
        r"\bkpn\b", r"\bziggo\b", r"\bvodafone\b", r"\bt-?mobile\b",
        r"\btelecom\b", r"\btelefonie\b", r"\bglas[- ]?vezel\b",
        r"\bfiber[- ]?install", r"\bfttx\b", r"\bnetwerk[- ]?aanleg",
    ], "telecom installer fleet"),

    ("delivery", [
        r"\bpostnl\b", r"\bdhl\b", r"\bdpd\b", r"\bups\b", r"\bgls\b",
        r"\bbring\b",
        # "courier" alone matches the Transit Courier model name; require context
        r"\bcourier\s*(?:fleet|service|company|van|vehicle|driver)\b",
        r"\bbezorg",
        r"\bdelivery\s*fleet\b", r"\bpakketdienst\b",
        r"\bpackage\s*delivery\b", r"\blast[- ]?mile\b",
    ], "courier / package delivery fleet"),

    ("solar", [
        r"\bsolar\b", r"\bzonnepan", r"\bphotovolta",
        r"\bpv[- ]?install", r"\bsolar\s*install",
    ], "solar panel installer"),

    ("refrigeration", [
        r"\bkoel\b", r"\bkühlfahr", r"\brefriger", r"\bfrigo\b",
        r"\bthermoking\b", r"\bcarrier\s*transicold",
    ], "refrigerated transport"),

    ("workshop", [
        r"\bsortimo\b", r"\bbott\b", r"\bmodul[- ]?system\b",
        r"\bworkshop\s*interior", r"\bservice\s*bus\b",
        r"\bservicewagen\b", r"\bwerkplaatsinrichting\b",
    ], "workshop / service van (fitted interior)"),

    ("construction", [
        r"\bbouw\b", r"\baannemer\b", r"\bloodgieter\b",
        r"\belektricien\b", r"\bschilder\b", r"\bmetselaar\b",
        r"\bdakdek", r"\bcontractor\b", r"\bbuilder\s*fleet",
        r"\bplumber\b", r"\bsteiger\b",
    ], "construction / contractor fleet"),

    ("hire", [
        r"\bex[- ]?rental\b", r"\bex[- ]?lease\b",
        r"\bleasing\s*return", r"\bverhuur\b",
        r"\brental\s*fleet", r"\bmietwagen\b",
    ], "ex-rental / ex-lease"),
]


def classify_fleet(*texts: Optional[str]) -> Tuple[str, List[str]]:
    """Return (fleet_type, evidence_keywords).

    Defaults to 'private' when no signal is found. Priority is the order of
    FLEET_PATTERNS — first hit wins, so curated tags like 'utility' override
    weaker construction signals.
    """
    haystack = " ".join(t for t in texts if t).lower()
    if not haystack.strip():
        return "private", []

    for label, patterns, _desc in FLEET_PATTERNS:
        matched: List[str] = []
        for pat in patterns:
            m = re.search(pat, haystack)
            if m:
                matched.append(m.group(0).strip())
        if matched:
            return label, matched

    return "private", []

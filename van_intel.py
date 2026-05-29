"""Van intelligence layer — deterministic 3-stage pipeline.

Stage 1: Hard filters     — remove non-van / damaged / illegal listings
Stage 2: Rule resolution  — global + model-specific year/km constraints
Stage 3: Scoring          — 0-100 desirability score

Single entry point: evaluate(vehicle) -> Evaluation
"""

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from models import ScoreBreakdown

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

SCORE_THRESHOLD = 30  # lots below this are soft-rejected (still logged)

# ---------------------------------------------------------------------------
# Van platform whitelist
# ---------------------------------------------------------------------------

# Maps model token (lowercased) → canonical "Make Model" label.
ALLOWED_MODELS: dict[str, str] = {
    "boxer":        "Peugeot Boxer",
    "ducato":       "Fiat Ducato",
    "jumper":       "Citroen Jumper",
    "transit":      "Ford Transit",
    "sprinter":     "Mercedes Sprinter",
    "master":       "Renault Master",
    "crafter":      "Volkswagen Crafter",
    "movano":       "Opel Movano",
    "tge":          "MAN TGE",
    "daily":        "Iveco Daily",
    "expert":       "Peugeot/Citroen Expert",   # mid-size panel van (optional)
    "transporter":  "Volkswagen Transporter",    # T5/T6 — common camper base
}

# Small van models routed to the "small" pipeline instead of being rejected.
# These are dual-use panel vans that make sense for crew + cargo use.
SMALL_VAN_MODELS: dict[str, str] = {
    "transit custom":   "Ford Transit Custom",
    "trafic":           "Renault Trafic",
    "vivaro":           "Opel Vivaro",
    "jumpy":            "Citroen Jumpy",
    "expert":           "Peugeot/Citroen Expert",    # also in ALLOWED_MODELS — overrides to small
    "transporter":      "Volkswagen Transporter",    # also in ALLOWED_MODELS — overrides to small
}

# Smaller siblings that disqualify a match even if a primary token hits.
# NOTE: transit custom / trafic / vivaro / jumpy are now in SMALL_VAN_MODELS
# so they must NOT appear here — they get their own route.
SMALLER_SIBLINGS: List[str] = [
    "vito", "citan", "v-klasse", "v klasse",
    "transit connect", "transit courier",
    "kangoo",
    "zafira", "combo",
    "berlingo", "partner",
    "caddy",
    "doblo", "fiorino", "talento", "scudo",
    "nemo", "bipper",
    # Expert traveller is a 9-seat minibus — not a cargo platform
    "expert traveller",
]

# ---------------------------------------------------------------------------
# Model-specific rule overrides (Stage 2)
# ---------------------------------------------------------------------------

_GLOBAL_RULES = {"min_year": 2014, "preferred_year": 2017, "label": "global"}

MODEL_RULES: dict[str, dict] = {
    "ducato":   {"min_year": 2016, "preferred_year": 2018, "label": "fiat_ducato_override"},
    "sprinter": {"min_year": 2015, "preferred_year": 2017, "label": "sprinter_override"},
    "master":   {"min_year": 2015, "preferred_year": 2017, "label": "master_override"},
    "boxer":        {"min_year": 2014, "preferred_year": 2016, "label": "boxer_override"},
    "jumper":       {"min_year": 2014, "preferred_year": 2016, "label": "jumper_override"},
    "expert":       {"min_year": 2016, "preferred_year": 2018, "label": "expert_override"},
    "transporter":  {"min_year": 2015, "preferred_year": 2017, "label": "transporter_override"},
}

def _rules_for(token: Optional[str]) -> dict:
    return MODEL_RULES.get(token or "", _GLOBAL_RULES)


# ---------------------------------------------------------------------------
# Stage 1: Hard-filter keyword lists
# ---------------------------------------------------------------------------

# 1.1 Vehicle type / body style exclusions
#
# Patterns are regex. Wrap single short tokens in ``\b…\b`` so we don't
# match inside longer unrelated words (e.g. "bus" inside "business",
# "coach" inside "approach", "tipper" inside "stripper", "crane" inside
# something else). Multi-word phrases ("tractor unit", "dump truck",
# "horse transport") are unambiguous as-is.
HARD_REJECT_TYPE: List[Tuple[str, str]] = [
    # trucks / heavy vehicles
    (r"\blorry\b|\blorries\b", "lorry"),
    (r"\btipper\b|\btippers\b", "tipper body"),
    (r"\bkipper\b|\bkippers\b", "tipper (NL/DE)"),
    (r"dump truck", "dump truck"),
    (r"\bdump\b", "dump body"),
    (r"tractor unit", "tractor unit"),
    (r"\btrekker\b|\btrekkers\b", "tractor unit (NL)"),
    (r"construction machine", "construction machinery"),
    (r"\bforklift\b|\bheftruck\b", "forklift"),
    (r"\bexcavator\b", "excavator"),
    (r"\bgraafmachine\b", "excavator (NL)"),
    (r"\bcrane\b|crane[\s-]?mounted", "crane mounted"),
    # vans converted / specialist bodies
    (r"\bambulance[a-z]*\b", "ambulance"),
    (r"\bziekenwagen\b", "ambulance (NL)"),
    (r"\bkrankenwagen\b", "ambulance (DE)"),
    (r"fire truck", "fire truck"),
    (r"\bbrandweer\b", "fire truck (NL)"),
    (r"\bfeuerwehr\b", "fire truck (DE)"),
    # ``bus`` needs strict word boundaries — "business", "abuse" etc.
    # would otherwise reject normal Boxer / Sprinter listings.
    (r"\bbus\b|\bbusses\b|\bbuses\b|\bminibus\b|\bschoolbus\b|\bschool bus\b|\bautobus\b|\breisbus\b", "bus / coach"),
    (r"\bcoach\b|\bcoaches\b", "coach"),
    (r"\bshuttle\b|\bshuttles\b", "passenger shuttle"),
    (r"\bhearse\b", "hearse"),
    (r"\blijkwagen\b", "hearse (NL)"),
    (r"horse transport", "horse transport"),
    (r"\bpaardentrailer\b|\bpaardenwagen\b", "horse transport (NL)"),
    (r"\bmotorhome\b|\bmotorhomes\b", "motorhome (pre-converted)"),
    (r"\bwohnmobil\b", "motorhome (DE)"),
    (r"\bmobilhome\b", "motorhome"),
    (r"\bcamper[a-z]*\b|\bkampeerwagen\b", "camper (pre-converted)"),
    # bundled lots (van + something else — pricing / logistics gets messy)
    (r"with trailer", "bundled with trailer"),
    (r"\+ trailer", "bundled with trailer"),
    (r"met aanhanger", "bundled with trailer (NL)"),
    (r"met aanhangwagen", "bundled with trailer (NL)"),
    (r"mit anhänger", "bundled with trailer (DE)"),
    (r"mit anhanger", "bundled with trailer (DE)"),
]

# 1.2 Body mismatches (cargo-platform only)
HARD_REJECT_BODY: List[Tuple[str, str]] = [
    (r"chassis[\s-]?cab", "chassis cab"),
    (r"chassis cabine", "chassis cab (NL)"),
    (r"light truck", "light truck (chassis variant)"),
    (r"\bflatbed\b", "flatbed body"),
    (r"\bopen bed\b", "open bed"),
    (r"\bdropside\b", "dropside body"),
    (r"platform truck", "platform truck"),
    (r"\bbakwagen\b", "box truck (NL)"),
    (r"\bbox truck\b", "box truck"),
    (r"\btipper\b", "tipper"),
    (r"\bpick[\s-]?up\b|\bpickup\b", "pickup"),
    (r"\brefrigerated\b", "refrigerated body"),
    (r"\bkoelwagen\b", "refrigerated (NL)"),
    (r"\bkühlfahrzeug\b", "refrigerated (DE)"),
    (r"\bfrigo\b", "refrigerated"),
    (r"\bice cream\b", "ice cream truck"),
    (r"\bijswagen\b", "ice cream truck (NL)"),
    (r"workshop interior", "workshop interior"),
    (r"\bwerkplaatsinrichting\b", "workshop interior (NL)"),
    (r"fully fitted", "fully fitted interior"),
    (r"volledig ingericht", "fully fitted interior (NL)"),
]

# 1.4 Extreme damage
HARD_REJECT_DAMAGE: List[Tuple[str, str]] = [
    # engine — negative lookbehind for "no/geen/kein" to avoid matching
    # "no engine failure codes" in OBD inspection reports.
    # Suffix (?!\s*code) excludes "engine failure code(s)" (OBD context).
    (r"(?<!no )(?<!No )(?<!geen )(?<!Geen )(?<!kein )(?<!Kein )engine failure(?!\s*codes?)", "engine failure"),
    (r"(?<!no )(?<!No )(?<!geen )(?<!Geen )engine broken", "engine failure"),
    (r"motor defect", "engine failure (NL)"),
    (r"motor kapot", "engine failure (NL)"),
    (r"motor stuk", "engine failure (NL)"),
    (r"motorschade", "engine failure (NL)"),
    (r"motorschaden", "engine failure (DE)"),
    # not running
    (r"non[\s-]?runner", "non-runner"),
    (r"not starting", "not starting"),
    (r"niet startend", "not starting (NL)"),
    (r"start niet", "not starting (NL)"),
    (r"startet nicht", "not starting (DE)"),
    (r"does not start", "not starting"),
    # gearbox
    (r"gearbox failure", "gearbox failure"),
    (r"gearbox broken", "gearbox failure"),
    (r"versnellingsbak defect", "gearbox failure (NL)"),
    (r"versnellingsbak kapot", "gearbox failure (NL)"),
    (r"getriebe defekt", "gearbox failure (DE)"),
    # fire / flood / structural
    (r"\bburned\b|\bburnt\b", "fire damage"),
    (r"fire damage", "fire damage"),
    (r"brandschade", "fire damage (NL)"),
    (r"brandschaden", "fire damage (DE)"),
    (r"flood damage", "flood damage"),
    (r"water damage", "flood damage"),
    (r"waterschade", "flood damage (NL)"),
    (r"wasserschaden", "flood damage (DE)"),
    (r"structural damage", "structural damage"),
    (r"total loss", "total loss"),
    (r"totalschade", "total loss (NL)"),
]

# Fuel hard reject — only when structured attribute explicitly confirms.
FUEL_HARD_REJECT = {"electric", "elektrisch", "elektro"}
FUEL_SOFT_PENALTY = {"cng", "lpg", "waterstof", "hydrogen"}

# ---------------------------------------------------------------------------
# Size detection
# ---------------------------------------------------------------------------
#
# Length (L1-L4) and height (H1-H3) are detected from a layered pipeline.
# Most lot titles do NOT carry an explicit "L3H2" marker (≤20% in
# practice), so we combine multiple signals at decreasing confidence:
#
#   1. explicit  — "L<n>H<m>" in title (highest)
#   2. explicit  — separate "L<n>" / "H<n>" in title
#   3. inferred  — roofline / wheelbase keywords ("high roof", "Maxi",
#                  "extra lang", "Hochdach", …)
#   4. inferred  — model-specific designation (e.g. Iveco Daily "35S" =
#                  short, "35L" = long)
#   5. guess     — weight_kg fallback per model family (only when length
#                  is otherwise unknown)
#   6. guess     — bodytype attribute fallback for height ("Box
#                  construction" → H2)
#
# The pipeline never returns a wildcard reject: it only rejects on a
# CONFIRMED L1 or H1 — wildcards/unknowns are passed through and
# downgraded by scoring instead.

_HEIGHT_KEYWORDS: List[Tuple[str, int, str]] = [
    (r"\b(super|extra|ultra)\s*(high|hoog|hoch)\s*(roof|dak|dach)\b", 3, "super-high roof"),
    (r"\bh\s*3\b|\bhochdach\s*3\b",                                    3, "H3 marker"),
    (r"\bhigh\s*roof\b|\bhoog\s*dak\b|\bhochdach\b",                   2, "high roof"),
    (r"\bmedium\s*roof\b|\bhalfhoog\b|\bmid\s*roof\b",                 2, "medium roof"),
    (r"\blow\s*roof\b|\bstandard\s*roof\b|\bnormaal\s*dak\b"
     r"|\bstandaard\s*dak\b|\bflach(es)?\s*dach\b",                    1, "low/standard roof"),
]

_LENGTH_KEYWORDS: List[Tuple[str, int, str]] = [
    (r"\bextra\s*(lang|long|lengte)\b|\bextralang\b|\bextralong\b",    4, "extra long"),
    (r"\bmaxi\b|\blwb\b|\blong\s*wheel\s*base\b|\blangwielbasis\b",    3, "long wheelbase"),
    (r"\bmwb\b|\bmedium\s*wheel\s*base\b|\bmidden\s*wielbasis\b",      2, "medium wheelbase"),
    (r"\bswb\b|\bshort\s*wheel\s*base\b|\bkort\s*wielbasis\b"
     r"|\bcompact\b",                                                   1, "short/compact wheelbase"),
]

# Per-model empty-weight thresholds (kg) → length band. Highly approximate
# — used only as a last-resort guess. None = skip (model doesn't fit
# the L1-L4 nomenclature, e.g. Iveco Daily / VW Transporter).
_WEIGHT_LENGTH_BANDS: dict = {
    "boxer":       (1900, 2050),   # <1900 → L1, 1900-2050 → L2, >2050 → L3
    "jumper":      (1900, 2050),
    "ducato":      (1900, 2050),
    "transit":     (1950, 2150),
    "sprinter":    (2100, 2300),
    "crafter":     (2100, 2300),
    "tge":         (2100, 2300),
    "master":      (2000, 2200),
    "movano":      (2000, 2200),
    # Daily, Transporter, Expert — skipped (different chassis / size class)
}


# Iveco Daily length-suffix codes after the GVW prefix
# e.g. "35S15" → S=short, "35L18" → L=long, "35C13" → chassis
_DAILY_LEN_MAP = {"S": 2, "L": 3}  # C falls into a separate body and is already rejected elsewhere


@dataclass
class SizeDetection:
    length: Optional[int]     # 1-4 or None
    height: Optional[int]     # 1-3 or None
    confidence: str           # "explicit" | "inferred" | "guess" | "unknown"
    evidence: List[str]
    status: str               # "accept" | "reject" | "unknown"

# ---------------------------------------------------------------------------
# Scoring constants
# ---------------------------------------------------------------------------

# Bonus keyword patterns (regex, bonus points, label)
BONUS_SIGNALS: List[Tuple[str, int, str]] = [
    (r"\b(airco|air\s*co|air\s*conditioning|klimaanlage)\b", 0, ""),  # note: no point value in spec
    (r"\bcrew\s*cab\b|crewcab|\bdubbele\s*cabine\b|\bdouble\s*cab\b|5\s*seat|5-seat|5\s*persoons|vijf\s*personen", 10, "crew_cab"),
    (r"\btrekhaak\b|tow\s*hitch|anhängerkupplung", 0, ""),  # no spec value
]

CREW_CAB_RE = re.compile(
    r"\bcrew\s*cab\b|crewcab|\bdubbele\s*cabine\b|\bdouble\s*cab\b"
    r"|5\s*seat|5-seat|5\s*persoons|vijf\s*personen|\bcombi\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

# ScoreBreakdown is defined in models.py (Pydantic) and imported above.
# Evaluation wraps it alongside the other pipeline outputs.

@dataclass
class Evaluation:
    passed_hard_filters: bool
    rejected_reason: Optional[str]
    van_type: Optional[str]
    score: Optional[int]              # 0-100
    breakdown: Optional[ScoreBreakdown]
    applied_rule_set: Optional[str]
    reasons: Optional[List[str]]


# ---------------------------------------------------------------------------
# Stage 1 helpers
# ---------------------------------------------------------------------------

def _check_list(haystack: str, pairs: List[Tuple[str, str]]) -> Optional[str]:
    """Run a list of (regex_pattern, label) against ``haystack`` case-insensitively
    and return the first matching label, or None."""
    s = haystack.lower()
    for pat, label in pairs:
        if re.search(pat, s, flags=re.IGNORECASE):
            return label
    return None


def _explicit_lh(s: str) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    """Tier 1: combined ``L<n>H<m>`` marker."""
    m = re.search(r"\bl\s*([1-4])\s*h\s*([1-3])\b", s)
    if not m:
        return None, None, None
    L, H = int(m.group(1)), int(m.group(2))
    return L, H, f"explicit L{L}H{H}"


def _explicit_l(s: str) -> Tuple[Optional[int], Optional[str]]:
    """Tier 2: standalone ``L<n>`` (where n in 1-4)."""
    m = re.search(r"\bl\s*([1-4])\b", s)
    if m:
        return int(m.group(1)), f"explicit L{m.group(1)}"
    return None, None


def _explicit_h(s: str) -> Tuple[Optional[int], Optional[str]]:
    """Tier 2: standalone ``H<n>`` (where n in 1-3)."""
    m = re.search(r"\bh\s*([1-3])\b", s)
    if m:
        return int(m.group(1)), f"explicit H{m.group(1)}"
    return None, None


def _height_from_keywords(s: str) -> Tuple[Optional[int], Optional[str]]:
    """Tier 3: roofline keywords. Returns first match in priority order."""
    for pat, h, label in _HEIGHT_KEYWORDS:
        if re.search(pat, s):
            return h, label
    return None, None


def _length_from_keywords(s: str) -> Tuple[Optional[int], Optional[str]]:
    """Tier 3: wheelbase keywords. Returns first match in priority order."""
    for pat, l, label in _LENGTH_KEYWORDS:
        if re.search(pat, s):
            return l, label
    return None, None


def _length_from_model_designation(s: str, model_token: Optional[str]) -> Tuple[Optional[int], Optional[str]]:
    """Tier 4: per-model designation parsing.

    Only Iveco Daily currently encodes length in its model number
    (e.g. ``35S15``, ``35L18`` — S=short, L=long). Other families'
    numeric prefixes encode GVW, not length, so we skip them."""
    if model_token != "daily":
        return None, None
    # Match a Daily designation like 35S, 50C, 70L etc. The first 2
    # digits are GVW (×100kg), the letter is the length code, and any
    # trailing digits are power.
    m = re.search(r"\b\d{2}([SLC])\d*\b", s, re.IGNORECASE)
    if not m:
        return None, None
    code = m.group(1).upper()
    L = _DAILY_LEN_MAP.get(code)
    if L is None:
        return None, None
    return L, f"Daily {code}-suffix → L{L}"


def _length_from_weight(model_token: Optional[str], weight_kg: Optional[int]) -> Tuple[Optional[int], Optional[str]]:
    """Tier 5: weight-band fallback. Only fires when we have an empty
    weight AND the model is in ``_WEIGHT_LENGTH_BANDS``."""
    if weight_kg is None or not model_token:
        return None, None
    bands = _WEIGHT_LENGTH_BANDS.get(model_token)
    if bands is None:
        return None, None
    low, high = bands
    if weight_kg < low:
        return 1, f"empty weight {weight_kg}kg < {low} → L1"
    if weight_kg < high:
        return 2, f"empty weight {weight_kg}kg in [{low},{high}) → L2"
    return 3, f"empty weight {weight_kg}kg ≥ {high} → L3"


def _height_from_bodytype(body_type: Optional[str]) -> Tuple[Optional[int], Optional[str]]:
    """Tier 6: bodytype attribute fallback. ``Box construction`` style
    panel vans almost always have standing-height H2."""
    if not body_type:
        return None, None
    s = body_type.lower()
    if "box" in s and ("construction" in s or "body" in s or "van" in s):
        return 2, f"bodytype '{body_type}' → H2"
    if any(w in s for w in ("closed van", "panel van", "kastenwagen", "bestelwagen", "furgon")):
        return 2, f"bodytype '{body_type}' → H2"
    return None, None


def _compose_van_type(L: Optional[int], H: Optional[int]) -> Optional[str]:
    """Compose the legacy ``van_type`` string from ``(L, H)``. Uses
    ``?`` for unknown dimensions; returns None when both are unknown."""
    if L is None and H is None:
        return None
    L_s = str(L) if L is not None else "?"
    H_s = str(H) if H is not None else "?"
    return f"L{L_s}H{H_s}"


def _detect_size(
    haystack: str,
    model_token: Optional[str] = None,
    weight_kg: Optional[int] = None,
    load_kg: Optional[int] = None,
    body_type: Optional[str] = None,
) -> SizeDetection:
    """Multi-signal length+height detection. See module-level comment for
    the tier pipeline."""
    s = haystack.lower()
    evidence: List[str] = []
    confidence = "unknown"

    # Tier 1 ─ explicit combined LxHy
    L, H, ev = _explicit_lh(s)
    if L is not None and H is not None:
        evidence.append(ev)
        confidence = "explicit"
    else:
        # Tier 2 ─ separate explicit L<n>, H<n>
        if L is None:
            L, ev = _explicit_l(s)
            if ev:
                evidence.append(ev); confidence = "explicit"
        if H is None:
            H, ev = _explicit_h(s)
            if ev:
                evidence.append(ev); confidence = "explicit"

        # Tier 3 ─ keyword inference
        if H is None:
            H, ev = _height_from_keywords(s)
            if ev:
                evidence.append(ev); confidence = "inferred" if confidence == "unknown" else confidence
        if L is None:
            L, ev = _length_from_keywords(s)
            if ev:
                evidence.append(ev); confidence = "inferred" if confidence == "unknown" else confidence

        # Tier 4 ─ model-specific designation (Iveco Daily)
        if L is None:
            L, ev = _length_from_model_designation(s, model_token)
            if ev:
                evidence.append(ev); confidence = "inferred" if confidence == "unknown" else confidence

        # Tier 5 ─ weight-band guess for length
        if L is None:
            L, ev = _length_from_weight(model_token, weight_kg)
            if ev:
                evidence.append(ev); confidence = "guess" if confidence == "unknown" else confidence

        # Tier 6 ─ bodytype attribute fallback for height
        if H is None:
            H, ev = _height_from_bodytype(body_type)
            if ev:
                evidence.append(ev); confidence = "guess" if confidence == "unknown" else confidence

    van_type = _compose_van_type(L, H)

    # Reject only on CONFIRMED (explicit/inferred) L1 or H1 — wildcards
    # and guess-confidence keep their soft pass and let scoring handle
    # the downgrade.
    if confidence in ("explicit", "inferred"):
        if L == 1 and H == 1:
            return SizeDetection(L, H, confidence, evidence, "reject")
        if L == 1 or H == 1:
            return SizeDetection(L, H, confidence, evidence, "reject")

    status = "accept" if (L is not None or H is not None) else "unknown"
    return SizeDetection(L, H, confidence, evidence, status)


def _sibling_match(haystack_lower: str) -> Optional[str]:
    """Return the name of a smaller-sibling model present in ``haystack_lower``
    as a whole word (single tokens) or whole multi-word phrase. Word boundaries
    avoid false positives like "partner" matching "business partner" or
    "trafic" matching "traffic"."""
    for sib in SMALLER_SIBLINGS:
        # Build a pattern that matches the sibling as a whole word/phrase.
        pat = r"\b" + r"\s+".join(re.escape(p) for p in sib.split()) + r"\b"
        if re.search(pat, haystack_lower):
            return sib
    return None


def _matched_model(haystack: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (canonical_name, token, pipeline) where pipeline is 'big' or 'small'.

    Small van models are checked first (multi-word tokens need priority over
    the single-word tokens in ALLOWED_MODELS — e.g. 'transit custom' must win
    over 'transit' alone).  Hard-reject siblings still return (None, None, None).
    """
    s = haystack.lower()
    if _sibling_match(s):
        return None, None, None
    # Small van models — multi-word first so "transit custom" beats "transit".
    for token in sorted(SMALL_VAN_MODELS, key=len, reverse=True):
        canonical = SMALL_VAN_MODELS[token]
        pat = r"\b" + r"\s+".join(re.escape(p) for p in token.split()) + r"\b"
        if re.search(pat, s):
            return canonical, token.replace(" ", "_"), "small"
    # Big van models
    for token, canonical in ALLOWED_MODELS.items():
        if token in ("expert", "transporter"):
            # These live in SMALL_VAN_MODELS now — skip in big pipeline
            continue
        if re.search(rf"\b{re.escape(token)}\b", s):
            return canonical, token, "big"
    return None, None, None


# ---------------------------------------------------------------------------
# Stage 3: Scoring helpers
# ---------------------------------------------------------------------------

def _score_year(year: Optional[int]) -> int:
    if year is None:
        return 0
    if year >= 2020:
        return 35   # was 30; +5 redistributed from VAT bonus
    if year >= 2017:
        return 27
    if year >= 2014:
        return 18
    return 0


def _score_mileage(km: Optional[int]) -> int:
    if km is None:
        return 0
    if km < 100_000:
        return 35   # was 30; +5 redistributed from VAT bonus
    if km <= 180_000:
        return 22
    if km <= 250_000:
        return 10
    return 0


# L/H scoring grid. L2H2 and L3H2 are the conversion sweet spot —
# enough cargo room for a camper build without being unwieldy. L4 and
# H3 are downweighted because they're awkward to drive, park, and
# convert (wind drag, hard to fit standard parking, weird interior
# proportions for camping). H1 is unusable for standing room. Wildcards
# get a middle band so we still rank partial knowledge above unknown.
_SIZE_GRID: dict = {
    ("1", "1"): 0, ("1", "2"): 5,  ("1", "3"): 3,  ("1", "?"): 3,
    ("2", "1"): 0, ("2", "2"): 18, ("2", "3"): 8,  ("2", "?"): 11,
    ("3", "1"): 0, ("3", "2"): 20, ("3", "3"): 8,  ("3", "?"): 12,
    ("4", "1"): 0, ("4", "2"): 8,  ("4", "3"): 3,  ("4", "?"): 5,
    ("?", "1"): 0, ("?", "2"): 10, ("?", "3"): 4,  ("?", "?"): 5,
}


def _score_van_size(van_type: Optional[str]) -> int:
    """Map a ``L<n>H<m>`` (or wildcard) van_type to its grid score.

    Accepts the legacy compact forms ("H2+", "L3", "PANEL", "L1") that
    pre-multi-signal detection produced — they're translated to the
    closest wildcard combo to keep historical entries scoring sanely."""
    s = (van_type or "").upper()

    m = re.match(r"L([1-4?])H([1-3?])$", s)
    if m:
        return _SIZE_GRID.get((m.group(1), m.group(2)), 5)

    # Legacy / pre-refactor codes — map to closest wildcard slot.
    legacy = {
        "H2+":   _SIZE_GRID[("?", "2")],
        "H3":    _SIZE_GRID[("?", "3")],
        "H2":    _SIZE_GRID[("?", "2")],
        "H1":    _SIZE_GRID[("?", "1")],
        "L3":    _SIZE_GRID[("3", "?")],
        "L2":    _SIZE_GRID[("2", "?")],
        "L1":    _SIZE_GRID[("1", "?")],
        "PANEL": _SIZE_GRID[("?", "2")],
    }
    if s in legacy:
        return legacy[s]

    return 5  # unknown — slight penalty


def _score_emission(emission_standard: Optional[str]) -> int:
    if not emission_standard:
        return 5  # neutral when unknown
    s = emission_standard.lower()
    if "euro 6" in s or "euro6" in s:
        return 10
    if "euro 5" in s or "euro5" in s:
        return 6
    if "euro 4" in s or "euro4" in s:
        return 3
    return 0  # Euro 3 or below


# Brand popularity for camper conversion (NL/EU market).
_RESALE_BRAND = {
    "ducato": 4, "boxer": 4, "jumper": 4,   # PSA triplets — biggest market
    "transit": 3, "sprinter": 3,             # very common, easy to resell
    "crafter": 2, "master": 2, "movano": 2, "daily": 2, "tge": 2,
    "expert": 1, "transporter": 1,
}


def _score_resaleability(
    model_token: Optional[str],
    emission_standard: Optional[str],
    condition: Optional[str],
) -> int:
    brand_pts = _RESALE_BRAND.get(model_token or "", 1)

    emission_pts = 0
    if emission_standard:
        s = emission_standard.lower()
        if "euro 6" in s or "euro6" in s:
            emission_pts = 4
        elif "euro 5" in s or "euro5" in s:
            emission_pts = 2
        elif "euro 4" in s or "euro4" in s:
            emission_pts = 1
    else:
        emission_pts = 2  # unknown — slightly penalised

    condition_pts = 0
    if condition == "WORKING":
        condition_pts = 2
    elif condition == "NOT_CHECKED":
        condition_pts = 1

    return min(brand_pts + emission_pts + condition_pts, 10)


def _build_reasons(
    canonical: str,
    van_type: Optional[str],
    size_evidence: Optional[str],
    bd: ScoreBreakdown,
    km: Optional[int],
    year: Optional[int],
    rule_label: str,
) -> List[str]:
    out = [f"{canonical} ({van_type or 'size unknown'})"]
    if size_evidence:
        out.append(f"size: {size_evidence}")
    if bd.mileage and (km is not None or year is not None):
        km_s = f"{km // 1000}k km" if km else "km ?"
        yr_s = str(year) if year else "year ?"
        out.append(f"{km_s}, {yr_s}")
    if bd.crew_cab:
        out.append("crew cab / 5-seat")
    out.append(f"rules: {rule_label}")
    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def evaluate(vehicle) -> Evaluation:
    title     = (getattr(vehicle, "title", None) or "")
    model_a   = (getattr(vehicle, "model", None) or "")
    remarks   = (getattr(vehicle, "remarks", None) or "")
    addl      = (getattr(vehicle, "additional_information", None) or "")
    fuel      = getattr(vehicle, "fuel", None)
    km        = getattr(vehicle, "km", None)
    year      = getattr(vehicle, "year", None)
    vat_margin = getattr(vehicle, "vat_margin", None)

    haystack = " ".join(s for s in [title, model_a, remarks, addl] if s)

    # ── Stage 1: Hard filters ────────────────────────────────────────────
    canonical, token, pipeline = _matched_model(haystack)
    if not canonical:
        sib = _sibling_match(haystack.lower())
        if sib:
            return Evaluation(False, f"smaller_sibling: {sib}", None, None, None, None, None)
        return Evaluation(False, "brand_not_whitelisted", None, None, None, None, None)

    r = _check_list(haystack, HARD_REJECT_TYPE)
    if r:
        return Evaluation(False, f"vehicle_type: {r}", None, None, None, None, None)

    r = _check_list(haystack, HARD_REJECT_BODY)
    if r:
        return Evaluation(False, f"body_mismatch: {r}", None, None, None, None, None)

    r = _check_list(haystack, HARD_REJECT_DAMAGE)
    if r:
        return Evaluation(False, f"damage: {r}", None, None, None, None, None)

    # Fuel — only hard-reject confirmed electric
    if fuel:
        s = fuel.strip().lower()
        for bad in FUEL_HARD_REJECT:
            if bad in s:
                return Evaluation(False, f"fuel_electric: {fuel}", None, None, None, None, None)

    # Mileage hard cap (1.3)
    if km is not None and km > 250_000:
        return Evaluation(False, f"mileage_too_high: {km}km", None, None, None, None, None)

    # Body size — multi-signal detection. Only confirmed L1/H1 reject;
    # unknown / wildcards pass through and let scoring downgrade them.
    det = _detect_size(
        haystack,
        model_token=token,
        weight_kg=getattr(vehicle, "weight_kg", None),
        load_kg=getattr(vehicle, "load_kg", None),
        body_type=getattr(vehicle, "body_type", None),
    )
    van_type = _compose_van_type(det.length, det.height)
    size_evidence = "; ".join(det.evidence) if det.evidence else None
    if det.status == "reject":
        return Evaluation(False, f"size_too_small: {size_evidence}", van_type, None, None, None, None)

    # ── Stage 2: Rule resolution ─────────────────────────────────────────
    rules = _rules_for(token)
    rule_label = rules["label"]

    if year is not None and year < rules["min_year"]:
        return Evaluation(
            False,
            f"year_below_minimum: {year} < {rules['min_year']} ({rule_label})",
            van_type, None, None, rule_label, None,
        )

    # ── Stage 3: Scoring ─────────────────────────────────────────────────
    bd = ScoreBreakdown(
        year        = _score_year(year),
        mileage     = _score_mileage(km),
        van_size    = _score_van_size(van_type),
        emission    = _score_emission(getattr(vehicle, "emission_standard", None)),
        resaleability = _score_resaleability(token, getattr(vehicle, "emission_standard", None), getattr(vehicle, "condition", None)),
        crew_cab    = 10 if CREW_CAB_RE.search(haystack) else 0,
    )
    score = bd.total()

    reasons = _build_reasons(canonical, van_type, size_evidence, bd, km, year, rule_label)

    if score < SCORE_THRESHOLD:
        return Evaluation(
            True,
            f"score_below_threshold: {score} < {SCORE_THRESHOLD}",
            van_type, score, bd, rule_label, reasons,
        )

    return Evaluation(True, None, van_type, score, bd, rule_label, reasons)


# ---------------------------------------------------------------------------
# Pipeline split: classify + per-pipeline scoring
# ---------------------------------------------------------------------------

# Big-van model tokens (camper-first pipeline)
_BIG_VAN_TOKENS = frozenset(
    t for t in ALLOWED_MODELS if t not in ("expert", "transporter")
)

# Small-van model tokens (dual-use pipeline).  These include the normalised
# forms stored in the vehicle dict after _matched_model returns them.
_SMALL_VAN_TOKENS = frozenset([
    "transit_custom", "trafic", "vivaro", "jumpy",
    "expert", "transporter",
])

# Models that sit on the big/small border and appear in BOTH pipelines
_DUAL_CATEGORY_MODELS = frozenset(["transit"])

# Ford Transit size combos that qualify it as big-van territory
_TRANSIT_BIG_SIZES = frozenset(["L3H2", "L3H3", "L4H2", "L4H3", "L3H?", "L4H?"])


def classify_vehicle(vehicle: dict) -> str:
    """Return 'big', 'small', or 'both' for a vehicle dict that has already
    passed hard filters (i.e. comes from the accepted list in run.py).

    The 'both' category is used for genuine midsize-ambiguous vehicles so they
    surface in both dashboard pages.
    """
    title    = (vehicle.get("title") or "").lower()
    remarks  = (vehicle.get("remarks") or "").lower()
    addl     = (vehicle.get("additional_information") or "").lower()
    haystack = " ".join([title, remarks, addl])

    # Check small van models first (multi-word tokens must beat single tokens)
    for token in sorted(SMALL_VAN_MODELS, key=len, reverse=True):
        pat = r"\b" + r"\s+".join(re.escape(p) for p in token.split()) + r"\b"
        if re.search(pat, haystack):
            canonical_token = token.replace(" ", "_")
            # Expert and Transporter — pure small
            if canonical_token in ("expert", "transporter"):
                return "small"
            return "small"

    # Ford Transit — special case: could be big (L3+) or small (L1/L2)
    if re.search(r"\btransit\b", haystack):
        van_type = vehicle.get("van_type") or ""
        if van_type in _TRANSIT_BIG_SIZES:
            return "big"
        # Unknown or small size → dual category so it appears in both
        return "both"

    # Big van models
    for token in _BIG_VAN_TOKENS:
        if re.search(rf"\b{re.escape(token)}\b", haystack):
            return "big"

    # Fallback — keep in big pipeline (should not happen for accepted lots)
    return "big"


# ── Big van scoring (camper-first) ──────────────────────────────────────────
#
# Weights (max points before clamping to 100):
#   A) Camper usability  40 pts — L/H sweet spot + cargo body
#   B) Build efficiency  25 pts — clean cargo box, no seats penalty
#   C) Mechanical        20 pts — mileage + year
#   D) Value             15 pts — deal_ratio


def _bvs_camper_usability(van_type: Optional[str], body_type: Optional[str]) -> int:
    """A) Camper usability — 0-40 pts."""
    s = (van_type or "").upper()
    score = 0

    # Sweet-spot grid
    _grid = {
        ("2", "2"): 35, ("3", "2"): 40,
        ("2", "3"): 20, ("3", "3"): 22,
        ("4", "2"): 18, ("4", "3"): 10,
        ("2", "?"): 22, ("3", "?"): 25,
        ("4", "?"): 12,
        ("1", "2"): 5,  ("1", "3"): 3,
        ("?", "2"): 20, ("?", "3"): 12,
        ("?", "?"): 10,
    }
    m = re.match(r"L([1-4?])H([1-3?])$", s)
    if m:
        score = _grid.get((m.group(1), m.group(2)), 10)
    else:
        score = 10  # unknown — moderate penalty

    # Cargo box body bonus: closed panel van = good insulation candidate
    bt = (body_type or "").lower()
    if any(kw in bt for kw in ("box", "closed van", "panel van", "kastenwagen", "bestelwagen", "furgon")):
        score = min(score + 3, 40)

    return min(score, 40)


def _bvs_build_efficiency(vehicle: dict) -> int:
    """B) Build efficiency — 0-25 pts.

    Rewards an empty cargo bay with no seats / no conversion interference.
    """
    pts = 15  # baseline — unknown = neutral
    seats = vehicle.get("seats")
    title_lower = (vehicle.get("title") or "").lower()
    remarks_lower = (vehicle.get("remarks") or "").lower()
    hay = title_lower + " " + remarks_lower

    if seats is not None:
        if seats <= 2:
            pts = 25   # driver-only or driver+passenger — empty bay, ideal
        elif seats <= 3:
            pts = 22
        elif seats <= 5:
            # crew cab — some conversion friction but seats can be removed
            pts = 12
        else:
            pts = 6    # many seats = a lot of removal work

    # Factory conversion signals (shelving, racking, workshop fit-out
    # that must be stripped) — small penalty but NOT a hard reject
    if re.search(r"\b(shelving|racking|inrichting|stellingkast|rek)\b", hay):
        pts = max(pts - 5, 0)

    return min(pts, 25)


def _bvs_mechanical(km: Optional[int], year: Optional[int]) -> int:
    """C) Mechanical baseline — 0-20 pts."""
    yr_pts = 0
    if year is not None:
        if year >= 2020:   yr_pts = 12
        elif year >= 2017: yr_pts = 9
        elif year >= 2014: yr_pts = 5
        else:              yr_pts = 0

    km_pts = 0
    if km is not None:
        if km < 80_000:    km_pts = 8
        elif km < 150_000: km_pts = 6
        elif km < 200_000: km_pts = 3
        else:              km_pts = 0

    return min(yr_pts + km_pts, 20)


def _bvs_value(deal_ratio: Optional[float]) -> int:
    """D) Value — 0-15 pts."""
    if deal_ratio is None:
        return 5   # neutral
    if deal_ratio >= 0.30:  return 15
    if deal_ratio >= 0.20:  return 12
    if deal_ratio >= 0.10:  return 8
    if deal_ratio >= 0.0:   return 5
    return 2   # slightly overpaying


def score_big_van(vehicle: dict) -> int:
    """Return a 0-100 camper-first suitability score for a big van."""
    van_type   = vehicle.get("van_type")
    body_type  = vehicle.get("body_type")
    km         = vehicle.get("km")
    year       = vehicle.get("year")
    deal_ratio = vehicle.get("deal_ratio")

    a = _bvs_camper_usability(van_type, body_type)
    b = _bvs_build_efficiency(vehicle)
    c = _bvs_mechanical(km, year)
    d = _bvs_value(deal_ratio)

    return min(a + b + c + d, 100)


# ── Small van scoring (dual-use-first) ──────────────────────────────────────
#
# Weights (max points before clamping to 100):
#   A) Dual-use utility    45 pts — seats, crew cab
#   B) City practicality   20 pts — size / length
#   C) Conversion potential 20 pts — fold-flat, sleeping layout signals
#   D) Value               15 pts — deal_ratio


_CREW_CAB_RE_SV = re.compile(
    r"\bcrew\s*cab\b|crewcab|\bdubbele\s*cabine\b|\bdouble\s*cab\b"
    r"|5\s*seat|5-seat|5\s*persoons|vijf\s*personen|\bcombi\b"
    r"|6\s*seat|6-seat|6\s*persoons|zes\s*personen",
    re.IGNORECASE,
)


def _svs_dual_use(vehicle: dict) -> int:
    """A) Dual-use utility — 0-45 pts.

    6 legal seats = 25 pts (MASSIVE), crew cab = 10 pts baseline.
    Factory anchor points or removable seat signals also score.
    """
    hay = " ".join(filter(None, [
        vehicle.get("title"), vehicle.get("remarks"),
        vehicle.get("additional_information"),
    ])).lower()

    seats = vehicle.get("seats")
    pts = 0

    # Seat count scoring
    if seats is not None:
        if seats >= 6:    pts = 38
        elif seats == 5:  pts = 28
        elif seats == 4:  pts = 18
        elif seats == 3:  pts = 10
        else:             pts = 2   # cargo only — low dual-use value for small van
    elif _CREW_CAB_RE_SV.search(hay):
        pts = 22   # strong signal even without explicit seat count

    # Anchor / removable seat signals — bonus on top
    if re.search(r"\b(anchor\s*point|rail|zitplaats|afneembare?\s*stoel|klapstoel)\b", hay):
        pts = min(pts + 7, 45)

    return min(pts, 45)


def _svs_city_practicality(van_type: Optional[str], fuel: Optional[str]) -> int:
    """B) City practicality — 0-20 pts.

    Shorter vans score better (L1>L2>L3). Diesel gets neutral, petrol/hybrid
    gets slight bonus for city zones.
    """
    s = (van_type or "").upper()
    size_pts = 10  # unknown — neutral

    m = re.match(r"L([1-4?])H([1-3?])$", s)
    if m:
        L = m.group(1)
        if L == "1":   size_pts = 18
        elif L == "2": size_pts = 15
        elif L == "3": size_pts = 10
        elif L == "4": size_pts = 4
        else:          size_pts = 10

    fuel_pts = 0
    f = (fuel or "").lower()
    if "hybrid" in f or "electric" in f:
        fuel_pts = 2
    elif "petrol" in f or "benzine" in f:
        fuel_pts = 1

    return min(size_pts + fuel_pts, 20)


def _svs_conversion_potential(vehicle: dict) -> int:
    """C) Conversion potential — 0-20 pts.

    Signals: fold-flat seats, sleeping layout references, bench-type seating,
    skylights/roof vent mentions, camper-prep mentions.
    """
    hay = " ".join(filter(None, [
        vehicle.get("title"), vehicle.get("remarks"),
        vehicle.get("additional_information"),
    ])).lower()

    pts = 8   # base — everything has some conversion potential
    if re.search(r"\b(fold[\s-]?flat|inklapbare?\s*stoel|fold\s*down\s*seat|klapstoel)\b", hay):
        pts = min(pts + 6, 20)
    if re.search(r"\b(sleeping|slaap|bed|matrass|matras|camperklaar|camper\s*ready)\b", hay):
        pts = min(pts + 6, 20)
    if re.search(r"\b(skylight|dakraam|panoramisch?\s*dak|glasdak)\b", hay):
        pts = min(pts + 3, 20)
    # Window van / kombi van signals — good candidate for conversion
    if re.search(r"\b(kombi|combi|glazen\s*zij|side\s*window\s*van)\b", hay):
        pts = min(pts + 3, 20)

    return min(pts, 20)


def _svs_value(deal_ratio: Optional[float]) -> int:
    """D) Value — 0-15 pts. Same logic as big van."""
    if deal_ratio is None:
        return 5
    if deal_ratio >= 0.30:  return 15
    if deal_ratio >= 0.20:  return 12
    if deal_ratio >= 0.10:  return 8
    if deal_ratio >= 0.0:   return 5
    return 2


def score_small_van(vehicle: dict) -> int:
    """Return a 0-100 dual-use-first suitability score for a small van."""
    van_type   = vehicle.get("van_type")
    fuel       = vehicle.get("fuel")
    deal_ratio = vehicle.get("deal_ratio")

    a = _svs_dual_use(vehicle)
    b = _svs_city_practicality(van_type, fuel)
    c = _svs_conversion_potential(vehicle)
    d = _svs_value(deal_ratio)

    return min(a + b + c + d, 100)

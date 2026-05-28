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

# Smaller siblings that disqualify a match even if a primary token hits.
SMALLER_SIBLINGS: List[str] = [
    "vito", "citan", "v-klasse", "v klasse",
    "transit connect", "transit courier", "transit custom",
    "trafic", "kangoo",
    "vivaro", "zafira", "combo",
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
    # engine
    (r"engine failure", "engine failure"),
    (r"engine broken", "engine failure"),
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

_SIZE_ACCEPT: List[Tuple[str, str, str]] = [
    (r"\bhigh\s*roof\b",          "H2+",   "high roof"),
    (r"\bhoog\s*dak\b",           "H2+",   "high roof (NL)"),
    (r"\bhochdach\b",             "H2+",   "high roof (DE)"),
    (r"\bmaxi\b",                 "L3",    "Maxi variant"),
    (r"\blwb\b",                  "L3",    "long wheelbase"),
    (r"\blong\s*wheel\s*base\b",  "L3",    "long wheelbase"),
    (r"\bextra\s*lang\b",         "L3",    "extra long (NL)"),
    (r"\bkastenwagen\b",          "panel", "panel van (DE)"),
    (r"\bbestelwagen\b",          "panel", "panel van (NL)"),
    (r"\bfurgon\b",               "panel", "panel van"),
]

_SIZE_REJECT: List[Tuple[str, str]] = [
    (r"\bswb\b",                  "short wheelbase"),
    (r"\bshort\s*wheel\s*base\b", "short wheelbase"),
    (r"\bcompact\b",              "compact variant"),
    (r"\bl1\b",                   "L1 (short)"),
    (r"\bl1\s*h1\b",              "L1H1"),
]

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


def _detect_size(haystack: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Return (status, van_type_code, evidence). status: accept|reject|unknown."""
    s = haystack.lower()

    # Explicit L_H_ code
    m = re.search(r"\bl\s*([1-4])\s*h\s*([1-3])\b", s)
    if m:
        L, H = int(m.group(1)), int(m.group(2))
        code = f"L{L}H{H}"
        if L == 1 or H == 1:
            return "reject", code, f"{code} (too small)"
        return "accept", code, f"explicit {code}"

    if re.search(r"\bh\s*[23]\b", s):
        return "accept", "H2+", "H2/H3 marker"
    if re.search(r"\bh\s*1\b", s) or re.search(r"\bl\s*1\b", s):
        return "reject", "H1/L1", "low/short roof marker"

    for pat, klass, label in _SIZE_ACCEPT:
        if re.search(pat, s):
            return "accept", klass, label

    for pat, label in _SIZE_REJECT:
        if re.search(pat, s):
            return "reject", "compact/SWB", label

    return "unknown", None, None


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


def _matched_model(haystack: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (canonical_name, token) or (None, None)."""
    s = haystack.lower()
    if _sibling_match(s):
        return None, None
    for token, canonical in ALLOWED_MODELS.items():
        if re.search(rf"\b{re.escape(token)}\b", s):
            return canonical, token
    return None, None


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


def _score_van_size(van_type: Optional[str]) -> int:
    s = (van_type or "").upper()
    if s in ("L4H3", "L3H3", "L4H2", "L3H2"):
        return 20
    if s == "L2H2":
        return 15
    if s in ("H2+", "H3", "L3", "PANEL"):
        return 10
    if s in ("L1H1", "H1", "L1"):
        return 0
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
    canonical, token = _matched_model(haystack)
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

    # Body size — reject only confirmed-small; unknown = soft pass
    size_status, van_type, size_evidence = _detect_size(haystack)
    if size_status == "reject":
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

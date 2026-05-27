"""Van intelligence layer.

Single entry point: ``evaluate(vehicle) -> Evaluation``.

Hard filters (any failure → reject with reason):
  - Brand whitelist (10 van platforms)
  - Smaller sibling rejection (Vito, Trafic, Vivaro, etc.)
  - Conversion-state reject (ambulance, camper, refrigerated, ...)
  - Damage reject (engine, gearbox, flood, fire, total loss)
  - Category reject (scooter, machinery, trailer)
  - Body size: L1H1 / SWB / compact → reject; unknown → soft pass
  - Fuel: electric / CNG / LPG → reject
  - Year: <2014 → reject (if year known)
  - Mileage: >250 000 km → reject (if km known)

Soft scoring (0–10 per axis, weighted total 0–10):
  geometry           0.25  — size class + crew-cab bonus
  modularity         0.30  — empty cargo / no fitout (key metric)
  conversion_friction 0.15 — friction signals in text
  mileage            0.20  — km + year combined
  eu_usability       0.10  — Euro norm + VAT deductibility

Equipment bonus (+0–2 on top of weighted total, capped at 10):
  AC, cruise control, tow hitch, rear camera each add +0.5.

Lots with total_score < SCORE_THRESHOLD are soft-rejected.
"""

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

SCORE_THRESHOLD = 7.0

WEIGHTS = {
    "geometry": 0.25,
    "modularity": 0.30,
    "conversion_friction": 0.15,
    "mileage": 0.20,
    "eu_usability": 0.10,
}

# ---------------------------------------------------------------------------
# Brand whitelist (10 van platforms)
# ---------------------------------------------------------------------------

ALLOWED_MODELS = {
    "boxer": "Peugeot Boxer",
    "ducato": "Fiat Ducato",
    "jumper": "Citroen Jumper",
    "transit": "Ford Transit",
    "sprinter": "Mercedes Sprinter",
    "master": "Renault Master",
    "crafter": "Volkswagen Crafter",
    "movano": "Opel Movano",
    "tge": "MAN TGE",
    "daily": "Iveco Daily",
}

# Phrases that disqualify even if a primary model token is present.
SMALLER_SIBLINGS = [
    # Mercedes smaller siblings
    "vito", "citan", "v-klasse", "v klasse",
    # Ford smaller siblings
    "transit connect", "transit courier",
    # Transit Custom is debatable (it IS large, but L2H1 typically)
    "transit custom",
    # Renault smaller siblings
    "trafic", "kangoo",
    # Opel / Vauxhall smaller siblings
    "vivaro", "zafira", "combo",
    # PSA smaller siblings
    "berlingo", "partner",
    # VW smaller siblings
    "caddy",
    # Fiat smaller siblings
    "doblo", "fiorino", "talento", "scudo",
    # Generic
    "nemo", "bipper",
]

# ---------------------------------------------------------------------------
# Damage — hard reject (scan haystack)
# ---------------------------------------------------------------------------

DAMAGE_REJECT = [
    ("engine broken", "engine failure"),
    ("engine failure", "engine failure"),
    ("motor defect", "engine failure (NL)"),
    ("motor kapot", "engine failure (NL)"),
    ("motor stuk", "engine failure (NL)"),
    ("motor broken", "engine failure"),
    ("motorschaden", "engine failure (DE)"),
    ("motorschade", "engine failure (NL)"),
    ("moteur cassé", "engine failure (FR)"),
    ("not starting", "not starting"),
    ("niet startend", "not starting (NL)"),
    ("start niet", "not starting (NL)"),
    ("startet nicht", "not starting (DE)"),
    ("does not start", "not starting"),
    ("starts not", "not starting"),
    ("gearbox broken", "gearbox failure"),
    ("gearbox defect", "gearbox failure"),
    ("gearbox failure", "gearbox failure"),
    ("versnellingsbak defect", "gearbox failure (NL)"),
    ("versnellingsbak kapot", "gearbox failure (NL)"),
    ("getriebe defekt", "gearbox failure (DE)"),
    ("water damage", "flood damage"),
    ("waterschade", "flood damage (NL)"),
    ("wasserschaden", "flood damage (DE)"),
    ("flood damage", "flood damage"),
    ("fire damage", "fire damage"),
    ("brandschade", "fire damage (NL)"),
    ("brandschaden", "fire damage (DE)"),
    ("total loss", "total loss"),
    ("totalschade", "total loss (NL)"),
    ("totalschaden", "total loss (DE)"),
]

# ---------------------------------------------------------------------------
# Conversion-state hard rejects
# ---------------------------------------------------------------------------

CONVERSION_REJECT = [
    ("ambulance", "ambulance build"),
    ("ambulanc", "ambulance build"),
    ("ziekenwagen", "ambulance (NL)"),
    ("ziekenauto", "ambulance (NL)"),
    ("krankenwagen", "ambulance (DE)"),
    ("refrigerated", "refrigerated body"),
    ("koelwagen", "refrigerated body (NL)"),
    ("koel-vries", "refrigerated body (NL)"),
    ("kühlfahrzeug", "refrigerated body (DE)"),
    ("frigo", "refrigerated body"),
    ("ice cream", "ice cream truck"),
    ("ijswagen", "ice cream truck (NL)"),
    ("werkplaatsinrichting", "workshop interior (NL)"),
    ("fully fitted", "fully fitted interior"),
    ("volledig ingericht", "fully fitted interior (NL)"),
    ("hearse", "hearse"),
    ("lijkwagen", "hearse (NL)"),
    ("school bus", "school bus"),
    ("schoolbus", "school bus"),
    ("paardentrailer", "horse transport"),
    ("horse transport", "horse transport"),
    ("paardenwagen", "horse transport (NL)"),
    ("minibus", "minibus / passenger shuttle"),
    ("shuttle", "passenger shuttle"),
    ("camper", "RV / camper conversion"),
    ("motorhome", "RV conversion"),
    ("mobilhome", "RV conversion"),
    ("wohnmobil", "RV conversion (DE)"),
    ("tipper", "tipper body"),
    ("kipper", "tipper body"),
    ("dumper", "dumper body"),
    ("pick-up", "pickup body"),
    ("pick up", "pickup body"),
    ("pickup", "pickup body"),
    ("chassis cab", "chassis cab"),
    ("chassis-cab", "chassis cab"),
    ("chassis cabine", "chassis cab (NL)"),
    ("light truck", "chassis cab variant"),
    ("flatbed", "flatbed body"),
    ("dropside", "dropside body"),
    ("tractor unit", "tractor unit"),
    ("trekker", "tractor unit (NL)"),
    ("crane", "crane mounted"),
    ("tow truck", "tow truck"),
    ("takelwagen", "tow truck (NL)"),
    ("box truck", "box truck"),
    ("bakwagen", "box truck (NL)"),
]

CATEGORY_REJECT = [
    ("scooter", "scooter"),
    ("motorcycle", "motorcycle"),
    ("trailer", "trailer"),
    ("aanhanger", "trailer (NL)"),
    ("forklift", "forklift"),
    ("excavator", "excavator"),
    ("graafmachine", "excavator (NL)"),
]

# Fuel types that are hard-rejected.
FUEL_REJECT = {"electric", "elektrisch", "elektro", "cng", "lpg", "waterstof", "hydrogen"}

# ---------------------------------------------------------------------------
# Body-size detection
# ---------------------------------------------------------------------------

SIZE_ACCEPT_HINTS = [
    (r"\bhigh\s*roof\b", "H2+", "high roof"),
    (r"\bhoog\s*dak\b", "H2+", "high roof (NL)"),
    (r"\bhochdach\b", "H2+", "high roof (DE)"),
    (r"\bmaxi\b", "L3", "Maxi variant"),
    (r"\blwb\b", "L3", "long wheelbase"),
    (r"\blong\s*wheel\s*base\b", "L3", "long wheelbase"),
    (r"\bextra\s*lang\b", "L3", "extra long (NL)"),
    (r"\blangwerkbasis\b", "L3", "long wheelbase (NL)"),
    (r"\bkastenwagen\b", "panel", "panel van (DE)"),
    (r"\bbestelwagen\b", "panel", "panel van (NL)"),
    (r"\bfurgon\b", "panel", "panel van (ES/PL)"),
]

SIZE_REJECT_HINTS = [
    (r"\bswb\b", "short wheelbase"),
    (r"\bshort\s*wheel\s*base\b", "short wheelbase"),
    (r"\bcompact\b", "compact variant"),
    (r"\bcity\s*van\b", "city van"),
    (r"\bl1\b", "L1 (short)"),
    (r"\bl1\s*h1\b", "L1H1 (compact)"),
]

# ---------------------------------------------------------------------------
# Friction / modularity indicators
# ---------------------------------------------------------------------------

FRICTION_INDICATORS = [
    ("wiring", 3, "wiring/electrical install"),
    ("installed", 2, "fitted install"),
    ("installation", 2, "fitted install"),
    ("inrichting", 3, "fitted interior (NL)"),
    ("inbouw", 3, "built-in (NL)"),
    ("furniture", 3, "fixed furniture"),
    ("meubilair", 3, "fixed furniture (NL)"),
    ("paneling", 3, "wall paneling"),
    ("panelled", 3, "wall paneling"),
    ("plywood", 2, "ply lining"),
    ("partition", 2, "partition wall"),
    ("schot", 2, "partition (NL)"),
    ("trennwand", 2, "partition (DE)"),
    ("rack", 1, "racking"),
    ("shelv", 1, "shelving"),
    ("stelling", 1, "shelving (NL)"),
]

EMPTY_HINTS = ("empty", "leeg", "kale laadruimte", "stripped", "no interior")

# Equipment signals (each boosts the equipment bonus).
EQUIPMENT_SIGNALS = [
    (r"\b(airco|air\s*conditioning|klimaanlage|air\s*co)\b", "AC"),
    (r"\bcruise\s*control\b", "cruise control"),
    (r"\b(tow\s*hitch|trekhaak|anhängerkupplung| anhangerkupplung)\b", "tow hitch"),
    (r"\b(rear\s*camera|achteruitrijcamera|rückfahrkamera|ruckfahrkamera)\b", "rear camera"),
    (r"\bnavigat", "navigation"),
    (r"\bparkeer\s*sensor|parking\s*sensor", "parking sensors"),
]

# Crew-cab / 5-seat keywords (major value bonus for legal conversion).
CREW_CAB_HINTS = [
    "crew cab", "crewcab", "double cabin", "dubbele cabine", "double cab",
    "5 seat", "5-seat", "5 persoons", "5-persoons", "vijf personen",
    "combi", "combiruimte",
]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class Evaluation:
    passed_hard_filters: bool
    rejected_reason: Optional[str]
    size_class: Optional[str]
    scores: Optional[dict]
    total_score: Optional[float]
    reasons: Optional[List[str]]


# ---------------------------------------------------------------------------
# Hard filter helpers
# ---------------------------------------------------------------------------

def _matched_model(haystack: str) -> Optional[str]:
    s = haystack.lower()
    for sib in SMALLER_SIBLINGS:
        if sib in s:
            return None
    for token, canonical in ALLOWED_MODELS.items():
        if re.search(rf"\b{re.escape(token)}\b", s):
            return canonical
    return None


def _detect_size(haystack: str) -> Tuple[str, Optional[str], Optional[str]]:
    s = haystack.lower()

    m = re.search(r"\bl\s*([1-4])\s*h\s*([1-3])\b", s)
    if m:
        L, H = int(m.group(1)), int(m.group(2))
        code = f"L{L}H{H}"
        if L == 1 or H == 1:
            return ("reject", code, f"size {code} (low/short)")
        return ("accept", code, f"explicit {code}")

    if re.search(r"\bh\s*[23]\b", s):
        return ("accept", "H2+", "H2/H3 marker")
    if re.search(r"\bh\s*1\b", s):
        return ("reject", "H1", "H1 (low roof)")
    if re.search(r"\bl\s*1\b", s):
        return ("reject", "L1", "L1 (short body)")

    for pat, klass, label in SIZE_ACCEPT_HINTS:
        if re.search(pat, s):
            return ("accept", klass, label)

    for pat, label in SIZE_REJECT_HINTS:
        if re.search(pat, s):
            return ("reject", "compact/SWB", label)

    # Unknown — soft pass (geometry will be penalised in scoring).
    return ("unknown", None, None)


def _conversion_reject(haystack: str) -> Optional[str]:
    s = haystack.lower()
    for kw, label in CONVERSION_REJECT:
        if kw in s:
            return label
    return None


def _category_reject(haystack: str) -> Optional[str]:
    s = haystack.lower()
    for kw, label in CATEGORY_REJECT:
        if kw in s:
            return label
    return None


def _damage_reject(haystack: str) -> Optional[str]:
    s = haystack.lower()
    for kw, label in DAMAGE_REJECT:
        if kw in s:
            return label
    return None


def _fuel_reject(fuel: Optional[str], haystack: str = "") -> Optional[str]:
    # Check structured fuel field first.
    if fuel:
        s = fuel.strip().lower()
        for bad in FUEL_REJECT:
            if bad in s:
                return f"fuel: {fuel}"
    # Also scan the full text — CNG/electric sometimes appears only in the title.
    h = haystack.lower()
    for bad in FUEL_REJECT:
        if re.search(rf"\b{re.escape(bad)}\b", h):
            return f"fuel in text: {bad}"
    return None


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _score_geometry(size_class: Optional[str], haystack: str) -> int:
    base = 5  # unknown
    if size_class:
        s = size_class.upper()
        if s in ("L4H3", "L3H3"):
            base = 10
        elif s in ("L3H2", "L4H2"):
            base = 9
        elif s == "L2H2":
            base = 8
        elif s in ("H2+", "H3", "L3"):
            base = 8
        elif s == "panel":
            base = 6

    # Crew cab / 5-seat bonus (+1, capped at 10).
    h = haystack.lower()
    if any(hint in h for hint in CREW_CAB_HINTS):
        base = min(base + 1, 10)

    return base


def _score_mileage(km: Optional[int], year: Optional[int]) -> int:
    km_score = 5
    if km is not None:
        if km < 80_000:
            km_score = 10
        elif km < 120_000:
            km_score = 9
        elif km < 180_000:
            km_score = 7
        elif km < 250_000:
            km_score = 5
        else:
            km_score = 2

    year_score = 5
    if year is not None:
        if year >= 2020:
            year_score = 10
        elif year >= 2017:
            year_score = 8
        elif year >= 2014:
            year_score = 6
        else:
            year_score = 3

    return round((km_score + year_score) / 2)


def _score_conversion_friction(haystack: str) -> Tuple[int, List[str]]:
    if not haystack:
        return 8, []
    s = haystack.lower()
    penalty = 0
    matched: List[str] = []
    for kw, weight, label in FRICTION_INDICATORS:
        if kw in s:
            penalty += weight
            if label not in matched:
                matched.append(label)
    return max(0, 10 - penalty), matched


def _score_modularity(haystack: str, friction_score: int) -> int:
    if haystack:
        s = haystack.lower()
        if any(kw in s for kw in EMPTY_HINTS):
            return 10
    if friction_score >= 10:
        return 9
    if friction_score >= 8:
        return 8
    if friction_score >= 6:
        return 6
    if friction_score >= 4:
        return 4
    return 2


def _score_eu_usability(emission_standard: Optional[str], vat_margin: Optional[bool]) -> int:
    base = 5
    if emission_standard:
        s = emission_standard.lower()
        if "euro 6" in s or "euro6" in s:
            base = 10
        elif "euro 5" in s or "euro5" in s:
            base = 7
        elif "euro 4" in s or "euro4" in s:
            base = 4
        elif re.search(r"euro\s*[123]\b", s):
            base = 2

    # VAT-deductible lot (marginGood=False) → buyer saves 21%.
    # Add +1 unless we're already at max.
    if vat_margin is False:
        base = min(base + 1, 10)

    return base


def _equipment_bonus(haystack: str) -> Tuple[float, List[str]]:
    """Each confirmed equipment item adds +0.5, max +2.0."""
    if not haystack:
        return 0.0, []
    s = haystack.lower()
    found: List[str] = []
    for pat, label in EQUIPMENT_SIGNALS:
        if re.search(pat, s):
            found.append(label)
    bonus = min(len(found) * 0.5, 2.0)
    return bonus, found


# ---------------------------------------------------------------------------
# Reasons / explanation
# ---------------------------------------------------------------------------

def _build_reasons(
    canonical_make: str,
    size_class: Optional[str],
    size_evidence: Optional[str],
    scores: dict,
    km: Optional[int],
    year: Optional[int],
    emission: Optional[str],
    friction_matches: List[str],
    equipment: List[str],
) -> List[str]:
    out = [f"{canonical_make} ({size_class or 'size unknown'})"]
    if size_evidence:
        out.append(f"size: {size_evidence}")
    if scores["mileage"] >= 7 and (km is not None or year is not None):
        km_part = f"{km // 1000}k km" if km is not None else "km unknown"
        yr_part = str(year) if year is not None else "yr unknown"
        out.append(f"mech: {km_part}, {yr_part}")
    if scores["eu_usability"] >= 7 and emission:
        out.append(f"emissions: {emission}")
    if scores["modularity"] >= 8:
        out.append("modularity: empty cargo shell")
    if friction_matches:
        out.append("friction: " + ", ".join(friction_matches[:3]))
    if equipment:
        out.append("equipment: " + ", ".join(equipment))
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def evaluate(vehicle) -> Evaluation:
    title = (getattr(vehicle, "title", None) or "")
    model_attr = (getattr(vehicle, "model", None) or "")
    remarks = (getattr(vehicle, "remarks", None) or "")
    additional = (getattr(vehicle, "additional_information", None) or "")
    fuel = getattr(vehicle, "fuel", None)
    km = getattr(vehicle, "km", None)
    year = getattr(vehicle, "year", None)
    emission = getattr(vehicle, "emission_standard", None)
    vat_margin = getattr(vehicle, "vat_margin", None)

    haystack = " ".join(s for s in [title, model_attr, remarks, additional] if s)

    # Hard filter 1: brand whitelist + smaller sibling
    canonical = _matched_model(haystack)
    if not canonical:
        s = haystack.lower()
        for sib in SMALLER_SIBLINGS:
            if sib in s:
                return Evaluation(False, f"smaller_sibling: {sib}", None, None, None, None)
        return Evaluation(False, "brand_not_whitelisted", None, None, None, None)

    # Hard filter 2: major damage
    dmg = _damage_reject(haystack)
    if dmg:
        return Evaluation(False, f"damage: {dmg}", None, None, None, None)

    # Hard filter 3: conversion-state
    conv = _conversion_reject(haystack)
    if conv:
        return Evaluation(False, f"fixed_conversion: {conv}", None, None, None, None)

    # Hard filter 4: category
    cat = _category_reject(haystack)
    if cat:
        return Evaluation(False, f"category_blacklisted: {cat}", None, None, None, None)

    # Hard filter 5: fuel type (structured field + title scan)
    fuel_bad = _fuel_reject(fuel, haystack)
    if fuel_bad:
        return Evaluation(False, f"fuel_rejected: {fuel_bad}", None, None, None, None)

    # Hard filter 6: year (reject only if year is known and clearly too old)
    if year is not None and year < 2014:
        return Evaluation(False, f"year_too_old: {year}", None, None, None, None)

    # Hard filter 7: mileage (reject only if km is known and too high)
    if km is not None and km > 250_000:
        return Evaluation(False, f"mileage_too_high: {km}km", None, None, None, None)

    # Hard filter 8: body size (confirmed reject only; unknown = soft pass)
    status, size_class, size_evidence = _detect_size(haystack)
    if status == "reject":
        return Evaluation(False, f"size_too_small: {size_evidence}", size_class, None, None, None)

    # Soft scoring.
    friction_score, friction_matches = _score_conversion_friction(haystack)
    eq_bonus, equipment_found = _equipment_bonus(haystack)

    scores = {
        "geometry": _score_geometry(size_class, haystack),
        "modularity": _score_modularity(haystack, friction_score),
        "conversion_friction": friction_score,
        "mileage": _score_mileage(km, year),
        "eu_usability": _score_eu_usability(emission, vat_margin),
    }

    weighted = sum(scores[k] * w for k, w in WEIGHTS.items())
    total = round(min(weighted + eq_bonus, 10), 2)

    reasons = _build_reasons(
        canonical, size_class, size_evidence, scores,
        km, year, emission, friction_matches, equipment_found,
    )

    if total < SCORE_THRESHOLD:
        return Evaluation(True, f"score_below_threshold: {total:.1f}", size_class, scores, total, reasons)

    return Evaluation(True, None, size_class, scores, total, reasons)

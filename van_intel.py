"""Van intelligence layer (Phase 2).

Geometry-first filter + 5-axis 0-10 scoring for camper-conversion candidates.
``evaluate(vehicle)`` is the single entry point; everything else is a helper.

Hard filters (must pass, else rejected with a structured reason):
  - brand whitelist: Fiat Ducato, Peugeot Boxer, Citroen Jumper, Ford Transit
  - smaller siblings rejected (Berlingo, Partner, Combo, Transit Connect, …)
  - body size: must confirm H2+ / L2+ / Maxi / LWB / high-roof
  - conversion state: ambulance, refrigerated, camper, workshop, chassis cab,
    tipper, pickup, hearse, shuttle/minibus, etc.

Soft filters: 5 axes scored 0-10, weighted into total_score (0-10). Lots
with total_score < SCORE_THRESHOLD also fail and land in the reject log.
"""

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

SCORE_THRESHOLD = 7.0

# Weights sum to 1.0 — modularity is the "key metric" per the geometry-first design.
WEIGHTS = {
    "geometry": 0.25,
    "modularity": 0.30,
    "conversion_friction": 0.15,
    "mileage": 0.20,
    "eu_usability": 0.10,
}

# ---------------------------------------------------------------------------
# Brand whitelist
# ---------------------------------------------------------------------------

# Model token (lowercased) -> canonical "Make Model" label.
ALLOWED_MODELS = {
    "boxer": "Peugeot Boxer",
    "ducato": "Fiat Ducato",
    "jumper": "Citroen Jumper",
    "transit": "Ford Transit",
}

# Phrases that, even if a primary model token is present, kick the lot out
# because they refer to a smaller sibling or different platform.
SMALLER_SIBLINGS = [
    "transit connect",
    "transit courier",
    "transit custom",  # custom is L1/L2 low roof — under our floor
    "berlingo",
    "partner",
    "combo",
    "doblo",
    "nemo",
    "bipper",
    "caddy",
    "kangoo",
]

# ---------------------------------------------------------------------------
# Conversion-state hard rejects (scan title + remarks + description)
# ---------------------------------------------------------------------------

CONVERSION_REJECT = [
    # ambulance
    ("ambulance", "ambulance build"),
    ("ambulanc", "ambulance build"),
    ("ziekenwagen", "ambulance (NL)"),
    ("ziekenauto", "ambulance (NL)"),
    ("krankenwagen", "ambulance (DE)"),
    # refrigerated
    ("refrigerated", "refrigerated body"),
    ("refrigerator", "refrigerated body"),
    ("koelwagen", "refrigerated body (NL)"),
    ("koel-vries", "refrigerated body (NL)"),
    ("kühlfahrzeug", "refrigerated body (DE)"),
    ("kuhlfahrzeug", "refrigerated body (DE)"),
    ("frigo", "refrigerated body"),
    ("ice cream", "ice cream truck"),
    ("ijswagen", "ice cream truck (NL)"),
    # service / workshop fitouts
    ("workshop interior", "workshop interior"),
    ("werkplaatsinrichting", "workshop interior (NL)"),
    ("fully fitted", "fully fitted interior"),
    ("volledig ingericht", "fully fitted interior (NL)"),
    # passenger
    ("hearse", "hearse"),
    ("lijkwagen", "hearse (NL)"),
    ("school bus", "school bus"),
    ("schoolbus", "school bus"),
    ("minibus", "minibus / passenger shuttle"),
    ("shuttle", "passenger shuttle"),
    # body conversions
    ("camper", "RV / camper conversion"),
    ("motorhome", "RV conversion"),
    ("mobilhome", "RV conversion"),
    ("mobil home", "RV conversion"),
    ("wohnmobil", "RV conversion (DE)"),
    ("tipper", "tipper body"),
    ("kipper", "tipper body (NL/DE)"),
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
    ("crane", "crane mounted"),
    ("tow truck", "tow truck"),
    ("recovery truck", "recovery truck"),
    ("takelwagen", "tow truck (NL)"),
    ("box truck", "box truck"),
    ("bakwagen", "box truck (NL)"),
]

# Category-style rejects (scan title)
CATEGORY_REJECT = [
    ("scooter", "scooter (category)"),
    ("motorcycle", "motorcycle (category)"),
    ("trailer", "trailer (category)"),
    ("aanhanger", "trailer (NL)"),
    ("forklift", "machinery (forklift)"),
    ("excavator", "machinery (excavator)"),
    ("graafmachine", "machinery (NL)"),
]

# ---------------------------------------------------------------------------
# Body-size detection (hard filter)
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
]

SIZE_REJECT_HINTS = [
    (r"\bswb\b", "short wheelbase"),
    (r"\bshort\s*wheel\s*base\b", "short wheelbase"),
    (r"\bcompact\b", "compact variant"),
    (r"\bkort\s*model\b", "short variant (NL)"),
    (r"\bcity\s*van\b", "city van variant"),
]

# ---------------------------------------------------------------------------
# Conversion friction (soft penalty — reduces modularity & friction scores)
# ---------------------------------------------------------------------------

FRICTION_INDICATORS = [
    # heavier signals (more confidence the van is fitted out)
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
    # lighter signals
    ("rack", 1, "racking"),
    ("shelv", 1, "shelving"),
    ("stelling", 1, "shelving (NL)"),
    ("ladder rack", 1, "ladder rack"),
    ("rooflight", 1, "roof window"),
    ("solar panel", 1, "solar panel install"),
]

EMPTY_HINTS = ("empty", "leeg", "kale laadruimte", "stripped", "no interior")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class Evaluation:
    passed_hard_filters: bool
    rejected_reason: Optional[str]
    size_class: Optional[str]
    scores: Optional[dict]  # axis -> 0..10 int
    total_score: Optional[float]
    reasons: Optional[List[str]]


# ---------------------------------------------------------------------------
# Hard filter helpers
# ---------------------------------------------------------------------------


def _matched_model(haystack: str) -> Optional[str]:
    """Return canonical "Make Model" if one of the allowed model tokens is in
    the haystack and no smaller-sibling phrase is."""
    s = haystack.lower()
    for sib in SMALLER_SIBLINGS:
        if sib in s:
            return None
    for token, canonical in ALLOWED_MODELS.items():
        # word boundary so "boxershort" wouldn't trip the boxer match
        if re.search(rf"\b{re.escape(token)}\b", s):
            return canonical
    return None


def _detect_size(haystack: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Return (status, size_class, evidence).

    status is one of "accept" / "reject" / "unknown".
    """
    s = haystack.lower()

    # Explicit L_H_ code (handles "L3H2", "L 3 H 2", "L3 H2", etc).
    m = re.search(r"\bl\s*([1-4])\s*h\s*([1-3])\b", s)
    if m:
        L, H = int(m.group(1)), int(m.group(2))
        code = f"L{L}H{H}"
        if L == 1 or H == 1:
            return ("reject", code, f"size {code} (low/short)")
        return ("accept", code, f"explicit {code}")

    # Standalone H2/H3 (after the L_H_ check so it doesn't override)
    if re.search(r"\bh\s*[23]\b", s):
        return ("accept", "H2+", "H2/H3 marker")
    if re.search(r"\bh\s*1\b", s):
        return ("reject", "H1", "H1 (low roof) marker")
    if re.search(r"\bl\s*1\b", s):
        return ("reject", "L1", "L1 (short) marker")

    for pat, klass, label in SIZE_ACCEPT_HINTS:
        if re.search(pat, s):
            return ("accept", klass, label)

    for pat, label in SIZE_REJECT_HINTS:
        if re.search(pat, s):
            return ("reject", "compact/SWB", label)

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


# ---------------------------------------------------------------------------
# Scoring helpers (each returns 0..10 int)
# ---------------------------------------------------------------------------


def _score_geometry(size_class: Optional[str]) -> int:
    if not size_class:
        return 5
    s = size_class.upper()
    if s in ("L4H3", "L3H3"):
        return 10
    if s in ("L3H2", "L4H2"):
        return 9
    if s == "L2H2":
        return 8
    if s in ("H2+", "H3", "L3"):
        return 8  # confirmed roof or length but missing the other axis
    return 5


def _score_mileage(km: Optional[int], year: Optional[int]) -> int:
    km_score = 5
    if km is not None:
        if km < 100_000:
            km_score = 10
        elif km < 150_000:
            km_score = 9
        elif km < 200_000:
            km_score = 7
        elif km < 250_000:
            km_score = 5
        elif km < 300_000:
            km_score = 3
        else:
            km_score = 1
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
    """Higher score = less friction. 10 = pristine."""
    if not haystack:
        return 8, []  # no info = slight uncertainty discount
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


def _score_eu_usability(emission_standard: Optional[str]) -> int:
    if not emission_standard:
        return 5
    s = emission_standard.lower()
    if "euro 6" in s or "euro6" in s:
        return 10
    if "euro 5" in s or "euro5" in s:
        return 7
    if "euro 4" in s or "euro4" in s:
        return 4
    if re.search(r"euro\s*[123]\b", s):
        return 2
    return 5


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
        out.append("modularity: likely empty cargo shell")
    if friction_matches:
        out.append("friction noted: " + ", ".join(friction_matches[:3]))
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def evaluate(vehicle) -> Evaluation:
    """Run hard filters + scoring against a parsed Vehicle (or dict-like).

    Accepts either a Vehicle model or any object exposing the same attrs.
    """
    title = (getattr(vehicle, "title", None) or "") or ""
    model_attr = (getattr(vehicle, "model", None) or "") or ""
    remarks = (getattr(vehicle, "remarks", None) or "") or ""
    additional = (getattr(vehicle, "additional_information", None) or "") or ""
    # The detail "Type" attribute often carries a sub-variant like "Boxer L3H2",
    # so feed it into the haystack alongside title and free-text fields.
    haystack = " ".join([title, model_attr, remarks, additional])

    # Hard filter 1: brand whitelist (+ smaller sibling check)
    canonical = _matched_model(haystack)
    if not canonical:
        # If we can identify it's a smaller sibling specifically, give a better reason.
        s = haystack.lower()
        for sib in SMALLER_SIBLINGS:
            if sib in s:
                return Evaluation(False, f"smaller_sibling: {sib}", None, None, None, None)
        return Evaluation(False, "brand_not_whitelisted", None, None, None, None)

    # Hard filter 2: conversion-state reject
    conv = _conversion_reject(haystack)
    if conv:
        return Evaluation(False, f"fixed_conversion: {conv}", None, None, None, None)

    # Hard filter 3: category reject
    cat = _category_reject(haystack)
    if cat:
        return Evaluation(False, f"category_blacklisted: {cat}", None, None, None, None)

    # Hard filter 4: body size
    status, size_class, size_evidence = _detect_size(haystack)
    if status == "reject":
        return Evaluation(False, f"size_too_small: {size_evidence}", size_class, None, None, None)
    if status == "unknown":
        return Evaluation(False, "size_unconfirmed", None, None, None, None)

    # All hard filters passed — score it.
    friction_score, friction_matches = _score_conversion_friction(haystack)
    scores = {
        "geometry": _score_geometry(size_class),
        "modularity": _score_modularity(haystack, friction_score),
        "conversion_friction": friction_score,
        "mileage": _score_mileage(getattr(vehicle, "km", None), getattr(vehicle, "year", None)),
        "eu_usability": _score_eu_usability(getattr(vehicle, "emission_standard", None)),
    }
    total = round(sum(scores[k] * w for k, w in WEIGHTS.items()), 2)

    reasons = _build_reasons(
        canonical,
        size_class,
        size_evidence,
        scores,
        getattr(vehicle, "km", None),
        getattr(vehicle, "year", None),
        getattr(vehicle, "emission_standard", None),
        friction_matches,
    )

    if total < SCORE_THRESHOLD:
        return Evaluation(
            True,
            f"score_below_threshold: {total:.1f}",
            size_class,
            scores,
            total,
            reasons,
        )

    return Evaluation(True, None, size_class, scores, total, reasons)

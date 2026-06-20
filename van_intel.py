"""Van intelligence layer — camper-candidate whitelist pipeline.

Flow:
    raw_listing
      → hard filters       (vehicle type / body / damage / fuel / mileage)
      → classify_vehicle   (must match one of 4 whitelist groups)
      → strict_filter      (size / year / Euro / seats — soft gate on unknowns)
      → scoring            (small-van suitability + ROI)

Single entry point: ``evaluate(vehicle) -> Evaluation``.

The model whitelist is intentionally narrow: only 4 small-van groups, all
L2 / L2H1, Euro 6, 6-seat-compatible. Big vans (Sprinter / Ducato / Boxer
/ etc.) are rejected outright by the classifier.

Soft-gate policy: unknown year / size / emission / seats PASSES through.
Only confirmed violations reject. The exception is the classifier itself:
no whitelist match → hard reject (``brand_not_in_whitelist``).
"""

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

SCORE_THRESHOLD = 30  # lots below this are soft-rejected (still logged)

# ---------------------------------------------------------------------------
# Whitelist groups (the only models that pass the classifier)
# ---------------------------------------------------------------------------
#
# Each group lists its model tokens (lowercased). Multi-word tokens are
# matched first so "transit custom" beats the bare "transit" (which is
# now NOT in the whitelist on its own — only Transit Custom qualifies).
#
# ``required_length`` and ``required_height`` are the allowed values
# (None = any). Detection is soft-gate: a CONFIRMED size that doesn't
# match rejects; unknown size passes.
#
# ``min_year`` is the lower bound for Euro 6 era for each model family.

WHITELIST_GROUPS: dict = {
    "transit_custom_l2h1": {
        "label": "Ford Transit Custom / Tourneo Custom",
        # Tourneo Custom is the passenger version of Transit Custom —
        # same body, factory 8-9 seats (great for 6-seat camper conversion).
        # Only the multi-word tokens belong here. The bare "transit" token now
        # routes to `ford_transit_l2h2` (the full-size Transit, which DOES come
        # in L2H2), and "transit custom"/"tourneo custom" still win for this
        # group via multi-word sort priority.
        "tokens": ["tourneo custom", "transit custom"],
        "required_length": [2],
        "required_height": [1],
        "min_year": 2016,
    },
    "expert_jumpy_proace_l2": {
        "label": "Peugeot Expert / Citroen Jumpy / Toyota ProAce (+ passenger Traveller / SpaceTourer / ProAce Verso)",
        # EMP2 platform. Cargo trio (Expert/Jumpy/ProAce) and passenger
        # trio (Traveller/SpaceTourer/ProAce Verso) share the same chassis,
        # same size codes, same min year — group them. The passenger trim
        # already comes insulated with windows and seats (faster camper
        # conversion), and gets an additional +15 score bonus downstream.
        "tokens": [
            "pro ace verso", "proace verso", "space tourer", "spacetourer",
            "pro ace", "proace", "traveller",
            "expert", "jumpy",
        ],
        "required_length": [2],
        "required_height": None,
        "min_year": 2016,
    },
    "scudo_gen3": {
        "label": "Fiat Scudo (gen 3, 2016+)",
        # Rebadged Expert/Jumpy/ProAce on the EMP2 platform. Gen 2 Scudo
        # (2006-2016) was a different vehicle but shares the Expert Gen 2
        # chassis — also valid, so min_year follows the expert_jumpy group
        # (2016). Pre-2016 Scudo rejects on Euro 4/5 emission anyway.
        "tokens": ["scudo"],
        "required_length": [2],
        "required_height": None,
        "min_year": 2016,
    },
    "vivaro_trafic_primastar_l2": {
        "label": "Opel Vivaro / Renault Trafic / Nissan Primastar / Fiat Talento",
        # Talento (2016-2021) is a rebadged Renault Trafic — same chassis,
        # same dimensions, same scoring.
        "tokens": ["talento", "vivaro", "trafic", "primastar"],
        "required_length": [2],
        "required_height": None,
        "min_year": 2015,
    },
    "t6_1_lwb": {
        "label": "VW Transporter T6 + T6.1 (Multivan / Caravelle / California)",
        # T6 (2015-2019) and T6.1 (2019.5+) share the LWB L2 chassis and
        # are both Euro 6 from launch. L1 (SWB) is also allowed — Caravelle
        # and Kombi variants in SWB are valid 6-seat adventure vans, just
        # slightly penalised in scoring (-10) vs L2 for shorter sleeping space.
        "tokens": ["t6.1", "t6_1", "transporter"],
        "required_length": [1, 2],
        "required_height": None,
        "min_year": 2015,
    },
    "psa_l1l2h1": {
        "label": "Peugeot Boxer / Fiat Ducato / Citroen Jumper (L2–L3, any height)",
        # Sevel-platform large van. L3H2 Ducato/Boxer is the EU camper gold
        # standard — it was wrong to exclude it. New rules:
        #   L1 (short, 2.8t) → reject (too small for practical camping)
        #   L2 or L3         → accepted (L3H2 is preferred)
        #   L4               → reject (too large / MAUT territory)
        #   H1 / H2 / H3    → all accepted (H3 is a valid full-stand-up height)
        # Unknown size → soft-pass (as always).
        # Min-year 2016 = Euro 6 era for this platform.
        "tokens": ["ducato", "jumper", "boxer"],
        "required_length": [2, 3],
        "required_height": None,
        "min_year": 2016,
    },
    "vito_v_class_l2": {
        "label": "Mercedes Vito / V-Class (Lang or Extralang)",
        # Vito (panel van) and V-Class (passenger MPV) share the W447
        # chassis since 2014. Three lengths: Kompakt (4895mm, L1-class),
        # Lang (5140mm, L2-class), Extralang (5370mm, matches Transit
        # Custom L2 best). required_length=[2,3,4] accepts Lang (no
        # keyword → unknown → soft-pass) AND Extralang (matches "extra
        # lang" → L4 in the shared keyword regex). The Kompakt variant
        # rejects via the explicit "kompakt" / "compact" → L1 detection.
        "tokens": ["v-klasse", "v klasse", "v-class", "vito"],
        "required_length": [2, 3, 4],
        "required_height": None,
        "min_year": 2015,
    },
    "hyundai_staria": {
        "label": "Hyundai Staria",
        # Korean passenger MPV (2021+). Single length (5253mm),
        # similar dimensional class to Transit Custom L2.
        "tokens": ["staria"],
        "required_length": None,
        "required_height": None,
        "min_year": 2021,
    },
    # -----------------------------------------------------------------------
    # High-roof panel-van families (L2H2 pivot — June 2026).
    # These big vans were previously rejected as brand_not_in_whitelist, but
    # they are the families that actually ship L2H2 (standing-height) — the
    # only dimensional class usable for the camper conversion. required_height
    # stays None (soft-gate: unknown height passes); the L2H2 feeds filter to
    # CONFIRMED H2/H3 via is_high_roof(). required_length=[2,3] accepts the
    # medium/long wheelbases and rejects L1 (too short) / L4 (jumbo) when the
    # length is confirmed.
    # -----------------------------------------------------------------------
    "ford_transit_l2h2": {
        "label": "Ford Transit (full-size, L2/L3 high-roof) — NOT Transit Custom",
        # Bare "transit". Transit Custom / Tourneo Custom are caught first by
        # their multi-word tokens; "transit connect" / "transit courier" reject
        # via SMALLER_SIBLINGS before reaching here.
        "tokens": ["transit"],
        "required_length": [2, 3],
        "required_height": None,
        "min_year": 2016,
    },
    "mercedes_sprinter": {
        "label": "Mercedes Sprinter (L2/L3 high-roof)",
        # Euro 6 from 2016 (W906 facelift / W907 from 2018). The classic
        # stand-up panel van; L2H2 / L3H2 are the prime conversion bases.
        "tokens": ["sprinter"],
        "required_length": [2, 3],
        "required_height": None,
        "min_year": 2016,
    },
    "vw_crafter_tge": {
        "label": "VW Crafter (gen 2) / MAN TGE",
        # Gen-2 Crafter (2017+) is VW's own platform (shared with MAN TGE),
        # Euro 6. The pre-2017 Crafter was Sprinter-based — min_year 2017
        # targets the new generation. e-Crafter is a valid EV base too.
        "tokens": ["e-crafter", "crafter", "tge"],
        "required_length": [2, 3],
        "required_height": None,
        "min_year": 2017,
    },
    "renault_master_grp": {
        "label": "Renault Master / Opel Movano / Nissan Interstar (NV400)",
        # Shared platform, Euro 6 from 2016. Movano/Interstar/NV400 are
        # rebadged Masters. L2H2 / L3H2 stand-up panel vans.
        "tokens": ["master", "movano", "interstar", "nv400"],
        "required_length": [2, 3],
        "required_height": None,
        "min_year": 2016,
    },
}

# Reverse index: token → (group_key, is_multiword). Built once.
_TOKEN_TO_GROUP: List[Tuple[str, str]] = []
for _gkey, _gdef in WHITELIST_GROUPS.items():
    for _tok in _gdef["tokens"]:
        _TOKEN_TO_GROUP.append((_tok, _gkey))
# Sort: multi-word first (longer phrases beat shorter ones), then by length desc
_TOKEN_TO_GROUP.sort(key=lambda x: (-(" " in x[0]), -len(x[0])))

# Flat set of all whitelist tokens — used by scraper.py for slug
# data-cleanup (drop redundant `model` attribute when it duplicates the
# token already inferable from the title).
WHITELIST_TOKENS: set = {tok for tok, _ in _TOKEN_TO_GROUP}


def is_high_roof(vehicle) -> bool:
    """True when the size code confirms a HIGH roof (H2 or H3).

    Reads the ``variant`` / ``van_type`` size code (e.g. "L2H2", "L3H2",
    "L?H2"). Unknown height ("L2H?", None) is NOT high-roof — we don't
    guess (same policy as size detection). Length-agnostic so L2H2 / L3H2
    / L3H3 all qualify. Used to build the high-roof auction + asking feeds."""
    if isinstance(vehicle, dict):
        var = vehicle.get("variant") or vehicle.get("van_type") or ""
    else:
        var = getattr(vehicle, "variant", None) or getattr(vehicle, "van_type", None) or ""
    return "H2" in var or "H3" in var


# Smaller siblings that disqualify a match even if a primary token hits.
# These are panel/utility vans we do NOT want even though they share a
# brand with whitelisted models (e.g. "Transit Connect" is a different
# vehicle from "Transit Custom").
#
# Models PREVIOUSLY here but now whitelisted (do not re-add):
#   - vito, v-klasse, v klasse  → now in `vito_v_class_l2`
#   - talento                    → now in `vivaro_trafic_primastar_l2`
#   - scudo                      → now in `scudo_gen3`
SMALLER_SIBLINGS: List[str] = [
    "citan",
    "transit connect", "transit courier",
    "kangoo",
    "zafira", "combo",
    "berlingo", "partner",
    "caddy",
    "doblo", "fiorino",
    "nemo", "bipper",
    "proace city",       # B-segment Berlingo-platform van, NOT the C-segment ProAce
    "expert traveller",  # 9-seat minibus variant of Expert
]


def _rules_for_group(group: Optional[str]) -> dict:
    """Return the rule dict for a whitelist group, or a permissive default."""
    if group and group in WHITELIST_GROUPS:
        return WHITELIST_GROUPS[group]
    return {"min_year": 2014, "required_length": None, "required_height": None, "label": "global"}


# ---------------------------------------------------------------------------
# Stage 1: Hard-filter keyword lists (unchanged from previous big-van era —
# damage / wrong-vehicle-type rejection still applies)
# ---------------------------------------------------------------------------

HARD_REJECT_TYPE: List[Tuple[str, str]] = [
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
    (r"\bambulance[a-z]*\b", "ambulance"),
    (r"\bziekenwagen\b", "ambulance (NL)"),
    (r"\bkrankenwagen\b", "ambulance (DE)"),
    (r"fire truck", "fire truck"),
    (r"\bbrandweer\b", "fire truck (NL)"),
    (r"\bfeuerwehr\b", "fire truck (DE)"),
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
    (r"with trailer", "bundled with trailer"),
    (r"\+ trailer", "bundled with trailer"),
    (r"met aanhanger", "bundled with trailer (NL)"),
    (r"met aanhangwagen", "bundled with trailer (NL)"),
    (r"mit anhänger", "bundled with trailer (DE)"),
    (r"mit anhanger", "bundled with trailer (DE)"),
]

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

HARD_REJECT_DAMAGE: List[Tuple[str, str]] = [
    (r"(?<!no )(?<!No )(?<!geen )(?<!Geen )(?<!kein )(?<!Kein )engine failure(?!\s*codes?)", "engine failure"),
    (r"(?<!no )(?<!No )(?<!geen )(?<!Geen )engine broken", "engine failure"),
    (r"motor defect", "engine failure (NL)"),
    (r"motor kapot", "engine failure (NL)"),
    (r"motor stuk", "engine failure (NL)"),
    (r"motorschade", "engine failure (NL)"),
    (r"motorschaden", "engine failure (DE)"),
    (r"non[\s-]?runner", "non-runner"),
    (r"not starting", "not starting"),
    (r"not drivable", "not drivable"),
    (r"niet rijdbaar", "not drivable (NL)"),
    (r"niet startend", "not starting (NL)"),
    (r"start niet", "not starting (NL)"),
    (r"startet nicht", "not starting (DE)"),
    (r"nicht fahrbereit", "not drivable (DE)"),
    (r"does not start", "not starting"),
    (r"gearbox failure", "gearbox failure"),
    (r"gearbox broken", "gearbox failure"),
    (r"versnellingsbak defect", "gearbox failure (NL)"),
    (r"versnellingsbak kapot", "gearbox failure (NL)"),
    (r"getriebe defekt", "gearbox failure (DE)"),
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

# Fuel — no hard rejects. Electric is explicitly allowed (eVito,
# eTransporter, e-Expert etc. are valid camper-conversion candidates;
# range/charging are buyer concerns, not classifier concerns).
# FUEL_SOFT_PENALTY tokens are informational only — currently unused
# in scoring; retained for future per-fuel weighting.
FUEL_SOFT_PENALTY = {"cng", "lpg", "waterstof", "hydrogen"}

# ---------------------------------------------------------------------------
# Size detection (unchanged tier pipeline — still useful for L2 confirmation)
# ---------------------------------------------------------------------------

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
    # Compact / short tokens — includes German "kompakt" which is how
    # Mercedes Vito and V-Class label their shortest variant (4895mm).
    (r"\bswb\b|\bshort\s*wheel\s*base\b|\bkort\s*wielbasis\b"
     r"|\bcompact\b|\bkompakt\b",                                       1, "short/compact wheelbase"),
]

# Weight-band fallback for length. Only used when no other length signal
# is present. The whitelisted models all sit in the small/mid-size class:
_WEIGHT_LENGTH_BANDS: dict = {
    # Peugeot Boxer / Fiat Ducato / Citroen Jumper — same Sevel platform.
    # L1H1 empty weight ~1650-1850kg, L2H1 ~1750-1950kg, L3H2 ~1900-2100kg.
    # Bands are approximate — use only as fallback when no explicit code found.
    "boxer":       (1850, 2050),
    "ducato":      (1850, 2050),
    "jumper":      (1850, 2050),
    # Renault Trafic / Opel Vivaro / Nissan Primastar / Fiat Talento — shared platform
    "trafic":      (1700, 1900),
    "vivaro":      (1700, 1900),
    "primastar":   (1700, 1900),
    "talento":     (1700, 1900),
    # Peugeot Expert / Citroen Jumpy / Toyota ProAce / Fiat Scudo gen-3 — EMP2 platform
    "expert":      (1600, 1800),
    "jumpy":       (1600, 1800),
    "proace":      (1600, 1800),
    "pro ace":     (1600, 1800),
    "scudo":       (1600, 1800),
    # Ford Transit Custom + Tourneo Custom: SWB ~1900kg, LWB ~2050kg
    "transit custom": (1900, 2050),
    "tourneo custom": (1900, 2050),
    # VW Transporter T5/T6: SWB ~1900kg, LWB ~2050kg
    "transporter": (1900, 2050),
    "t6.1":        (1900, 2050),
    "t6_1":        (1900, 2050),
    # Mercedes Vito / V-Class W447: Kompakt ~1900kg, Lang ~2000kg, Extralang ~2100kg.
    # Bands push Extralang into L3 here (the Vito group accepts both L2 and L3,
    # and the small-van clamp is suppressed for this group — see classify_vehicle).
    "vito":        (1950, 2080),
    "v-klasse":    (1950, 2080),
    "v klasse":    (1950, 2080),
    "v-class":     (1950, 2080),
    # Hyundai Staria — single length (5253mm), so weight fallback is moot
    # but included for completeness so unknown-length lots still get a signal.
    "staria":      (2100, 2300),
}


@dataclass
class SizeDetection:
    length: Optional[int]     # 1-4 or None
    height: Optional[int]     # 1-3 or None
    confidence: str           # "explicit" | "inferred" | "guess" | "unknown"
    evidence: List[str]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class Classification:
    """Output of ``classify_vehicle(title, description)``.

    ``group`` is one of the keys of ``WHITELIST_GROUPS`` or None when no
    whitelist token matched. ``variant`` is a compact L/H string like
    "L2H1" / "L2" / "L2H?" derived from size detection. ``confidence``
    grades the match: "high" (explicit token + explicit size), "medium"
    (inferred size), "low" (token but no size signal), "unknown" (no
    classifier hit at all → caller must reject)."""
    group: Optional[str]
    variant: Optional[str]
    confidence: str
    matched_token: Optional[str] = None
    evidence: Optional[List[str]] = None


@dataclass
class Evaluation:
    passed_hard_filters: bool
    rejected_reason: Optional[str]
    van_type: Optional[str]
    model_group: Optional[str]
    variant: Optional[str]
    classification_confidence: Optional[str]
    score: Optional[int]
    applied_rule_set: Optional[str]
    reasons: Optional[List[str]]


# ---------------------------------------------------------------------------
# Stage 1 helpers
# ---------------------------------------------------------------------------

def _check_list(haystack: str, pairs: List[Tuple[str, str]]) -> Optional[str]:
    """Run regex pairs against ``haystack`` case-insensitively; return first label."""
    s = haystack.lower()
    for pat, label in pairs:
        if re.search(pat, s, flags=re.IGNORECASE):
            return label
    return None


def _explicit_lh(s: str) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    # Use (?!\d) instead of \b so "L2H1TDC" (Ford's Transit Double Cab suffix)
    # and similar manufacturer codes are captured correctly — \b fails when
    # the height digit is immediately followed by another word character.
    m = re.search(r"\bl\s*([1-4])\s*h\s*([1-3])(?!\d)", s, re.IGNORECASE)
    if not m:
        return None, None, None
    L, H = int(m.group(1)), int(m.group(2))
    return L, H, f"explicit L{L}H{H}"


def _explicit_l(s: str) -> Tuple[Optional[int], Optional[str]]:
    # (?!\s*h) avoids matching the L in "L2H1" as a standalone L2.
    m = re.search(r"\bl\s*([1-4])(?!\s*h)(?!\d)", s, re.IGNORECASE)
    if m:
        return int(m.group(1)), f"explicit L{m.group(1)}"
    return None, None


def _explicit_h(s: str) -> Tuple[Optional[int], Optional[str]]:
    m = re.search(r"\bh\s*([1-3])(?!\d)", s, re.IGNORECASE)
    if m:
        return int(m.group(1)), f"explicit H{m.group(1)}"
    return None, None


def _height_from_keywords(s: str) -> Tuple[Optional[int], Optional[str]]:
    for pat, h, label in _HEIGHT_KEYWORDS:
        if re.search(pat, s):
            return h, label
    return None, None


def _length_from_keywords(s: str) -> Tuple[Optional[int], Optional[str]]:
    for pat, l, label in _LENGTH_KEYWORDS:
        if re.search(pat, s):
            return l, label
    return None, None


def _length_from_weight(model_token: Optional[str], weight_kg: Optional[int]) -> Tuple[Optional[int], Optional[str]]:
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
    if not body_type:
        return None, None
    s = body_type.lower()
    if "box" in s and ("construction" in s or "body" in s or "van" in s):
        return 2, f"bodytype '{body_type}' → H2"
    if any(w in s for w in ("closed van", "panel van", "kastenwagen", "bestelwagen", "furgon")):
        return 2, f"bodytype '{body_type}' → H2"
    return None, None


def _compose_variant(L: Optional[int], H: Optional[int]) -> Optional[str]:
    if L is None and H is None:
        return None
    L_s = str(L) if L is not None else "?"
    H_s = str(H) if H is not None else "?"
    return f"L{L_s}H{H_s}"


def _detect_size(
    haystack: str,
    model_token: Optional[str] = None,
    weight_kg: Optional[int] = None,
    body_type: Optional[str] = None,
) -> SizeDetection:
    """Detect L/H from EXPLICIT codes only (e.g. ``L2H1``, ``\\bL2\\b``, ``\\bH1\\b``).

    Heuristic inference — roofline keywords (high roof → H2), length keywords
    (LWB → L3), empty-weight bands, body-type slugs — used to fill in
    "probably L2 / H2" guesses, but those false-positives outweighed the
    signal: they pushed otherwise-valid lots into size_not_allowed rejection
    when nothing in the listing actually claimed an out-of-spec size.

    Soft-gate philosophy now applied to size end-to-end: unknown = soft-pass.
    The variant string is set only when the seller wrote a literal L/H code.
    Weight / body_type / model_token arguments are accepted for backward
    compatibility but no longer consulted.
    """
    s = haystack.lower()
    evidence: List[str] = []
    confidence = "unknown"

    L, H, ev = _explicit_lh(s)
    if L is not None and H is not None:
        evidence.append(ev)
        confidence = "explicit"
    else:
        if L is None:
            L, ev = _explicit_l(s)
            if ev:
                evidence.append(ev); confidence = "explicit"
        if H is None:
            H, ev = _explicit_h(s)
            if ev:
                evidence.append(ev); confidence = "explicit"

    return SizeDetection(L, H, confidence, evidence)


def _sibling_match(haystack_lower: str) -> Optional[str]:
    for sib in SMALLER_SIBLINGS:
        pat = r"\b" + r"\s+".join(re.escape(p) for p in sib.split()) + r"\b"
        if re.search(pat, haystack_lower):
            return sib
    return None


def _normalize_electric_variants(s: str) -> str:
    """Strip electric-variant prefixes/suffixes so the token matcher sees
    the base model name. Handles common manufacturer styling:
      eVito / e-Vito          → Vito
      e-Transporter           → Transporter
      Vivaro-e                → Vivaro
      Trafic E-Tech           → Trafic
      Transit Custom Plug-in  → Transit Custom
    Without this, ``\\bvito\\b`` fails to match the glued "eVito" form
    used in Mercedes lot titles."""
    out = s
    for token in WHITELIST_TOKENS:
        if " " in token or "." in token:
            continue   # only single-word tokens are subject to e-prefix glue
        # Leading "e-?" (electric prefix) — handles "eVito", "e-Vito"
        out = re.sub(rf"\be-?({re.escape(token)})\b", r"\1", out, flags=re.IGNORECASE)
        # Trailing "-e" suffix — handles "Vivaro-e"
        out = re.sub(rf"\b({re.escape(token)})-e\b", r"\1", out, flags=re.IGNORECASE)
        # Trailing " E-Tech" / " e-tech" — Renault's electric trim name
        out = re.sub(rf"\b({re.escape(token)})\s+e-?tech\b", r"\1", out, flags=re.IGNORECASE)
    return out


def _match_whitelist_token(haystack_lower: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (token, group_key) for the first whitelist match, multi-word
    tokens prioritised. Returns (None, None) when no token matches."""
    for token, group in _TOKEN_TO_GROUP:
        if " " in token or "." in token:
            # phrase / dotted token — match as whole phrase
            parts = re.split(r"[\s.]+", token)
            pat = r"\b" + r"\s*\.?\s*".join(re.escape(p) for p in parts) + r"\b"
        else:
            pat = rf"\b{re.escape(token)}\b"
        if re.search(pat, haystack_lower):
            return token, group
    return None, None


# ---------------------------------------------------------------------------
# Public: classify_vehicle (per spec signature)
# ---------------------------------------------------------------------------

def classify_vehicle(
    title: str,
    description: str = "",
    *,
    weight_kg: Optional[int] = None,
    body_type: Optional[str] = None,
) -> Classification:
    """Classify a listing against the whitelist groups.

    Returns ``Classification(group, variant, confidence, matched_token,
    evidence)``. ``group=None`` and ``confidence='unknown'`` when no
    whitelist token matched — the caller MUST treat this as a rejection.

    ``weight_kg`` and ``body_type`` are optional structured-data inputs
    that improve size detection when title/description alone are silent.
    """
    haystack = " ".join(s for s in (title, description) if s)
    s = haystack.lower()

    # Normalize electric-variant naming (eVito → Vito, Vivaro-e → Vivaro,
    # Trafic E-Tech → Trafic, etc.) so the token matcher's word-boundary
    # regex isn't defeated by the glued "e" prefix in manufacturer styling.
    s = _normalize_electric_variants(s)

    # Smaller-sibling reject takes priority — Transit Connect, Vito,
    # Caddy, etc. must NOT classify as a whitelist match even if a
    # whitelist token also appears.
    sib = _sibling_match(s)
    if sib:
        return Classification(
            group=None, variant=None, confidence="unknown",
            matched_token=None, evidence=[f"smaller_sibling: {sib}"],
        )

    token, group = _match_whitelist_token(s)
    if not group:
        return Classification(
            group=None, variant=None, confidence="unknown",
            matched_token=None, evidence=None,
        )

    det = _detect_size(
        haystack, model_token=token, weight_kg=weight_kg, body_type=body_type,
    )

    variant = _compose_variant(det.length, det.height)

    if det.confidence == "explicit":
        confidence = "high"
    elif det.confidence == "inferred":
        confidence = "medium"
    elif det.confidence == "guess":
        confidence = "low"
    else:
        confidence = "low"

    evidence = [f"token: {token}"] + det.evidence
    return Classification(
        group=group, variant=variant, confidence=confidence,
        matched_token=token, evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Public: strict_filter
# ---------------------------------------------------------------------------

def strict_filter(vehicle, classification: Classification) -> Tuple[bool, Optional[str]]:
    """Apply the camper-candidate hard gate to a classified vehicle.

    Returns ``(passed, rejected_reason)``. Soft-gate policy: only
    *confirmed* violations of size / year / Euro reject. Unknown
    values pass. The classifier itself is a hard gate — group=None
    rejects unconditionally as ``brand_not_in_whitelist``.

    Seats are NOT gated (June 2026 L2H2 pivot): cargo panel vans are the
    desirable conversion bases, and a 6-seat crew cab is a scoring bonus,
    not a requirement. The per-group size gate always applies — a confirmed
    length/height outside the group's allowed set rejects.
    """
    if classification.group is None:
        return False, "brand_not_in_whitelist"

    rules = WHITELIST_GROUPS[classification.group]

    # Size — parse the variant string back to L/H ints. Soft gate: only a
    # CONFIRMED length/height outside the group's allowed set rejects;
    # unknown dimensions ("?") pass.
    if classification.variant:
        m = re.match(r"L([1-4?])H([1-3?])$", classification.variant)
        if m:
            L = None if m.group(1) == "?" else int(m.group(1))
            H = None if m.group(2) == "?" else int(m.group(2))
            req_L = rules.get("required_length")
            req_H = rules.get("required_height")
            if L is not None and req_L is not None and L not in req_L:
                return False, f"size_not_allowed: L{L} (group requires L{'/'.join(map(str, req_L))})"
            if H is not None and req_H is not None and H not in req_H:
                return False, f"size_not_allowed: H{H} (group requires H{'/'.join(map(str, req_H))})"

    # Year — soft gate: only reject confirmed below min_year
    year = getattr(vehicle, "year", None) if not isinstance(vehicle, dict) else vehicle.get("year")
    min_year = rules.get("min_year")
    if year is not None and min_year is not None and year < min_year:
        return False, f"year_below_minimum: {year} < {min_year} ({classification.group})"

    # Emission — soft gate: only reject confirmed Euro 3/4/5
    emission = (
        getattr(vehicle, "emission_standard", None)
        if not isinstance(vehicle, dict)
        else vehicle.get("emission_standard")
    )
    if emission:
        es = str(emission).lower()
        if re.search(r"\beuro\s*[12345]\b|\beuro[12345]\b", es) and "euro 6" not in es and "euro6" not in es:
            return False, f"emission_below_euro6: {emission}"

    return True, None


# Cargo-only body type slugs/labels seen across sources. Used as a
# soft-gate seats-fallback for vans where the title carries no explicit
# seat count and the listing doesn't expose `seats`.
_CARGO_BODY_TYPES = {
    "bedrijfswagen",          # autotrack carrosserievormSlug
    "bestelwagen",            # marktplaats / 2dehands
    "bestelauto",
    "kastenwagen",            # german panel van
    "gesloten bestelwagen",
    "closed van", "panel van",
    "furgon",                 # spanish
}


def _is_cargo_body(body_type) -> bool:
    if not body_type:
        return False
    return str(body_type).lower() in _CARGO_BODY_TYPES


_SEATS_FROM_TEXT_RE = re.compile(
    r"\b([2-9])\s*[-–]?\s*"
    r"(?:persoons?|personen|zits?|zit(?:plaatsen)?|seater?s?|sitze?r?|places?)\b",
    re.IGNORECASE,
)

# Dutch shorthand: "2p", "3p.", "4P" — common in Marktplaats / AutoTrack
# Peugeot Expert / Boxer titles ("120pk 2p. STT Verh."). The `\b` after
# `p` correctly excludes "120pk" (no word boundary between P and K). Limit
# to a single digit 2-9 to avoid matching trim codes like "T29".
_SEATS_NP_SHORTHAND_RE = re.compile(r"\b([2-9])\s*p\b", re.IGNORECASE)


def _seats_from_text(text: Optional[str]) -> Optional[int]:
    """Return seat count if explicitly stated in ``text`` (e.g. "3-persoons",
    "5 seater", "2p", "2 zits"), else None. Multi-language (NL/EN/DE/FR)."""
    if not text:
        return None
    m = _SEATS_FROM_TEXT_RE.search(text) or _SEATS_NP_SHORTHAND_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Stage 3: Small-van scoring (kept from previous era — still the right
# shape for the camper-candidate models, all of which are small vans)
# ---------------------------------------------------------------------------

_CREW_CAB_RE = re.compile(
    r"\bcrew\s*cab\b|crewcab|\bdubbele?\s*cabine?\b|\bdouble\s*cab\b"
    r"|\bdc\b|\bd\.c\.\b"
    r"|5\s*seat|5-seat|5\s*persoons|vijf\s*personen|\bcombi\b"
    r"|6\s*seat|6-seat|6\s*persoons|zes\s*personen",
    re.IGNORECASE,
)


def _svs_dual_use(vehicle: dict) -> int:
    """Dual-use utility — 0-30 pts.  Seats are a BONUS, not the headline.

    Post-pivot (June 2026) the decisive factor is roof height, not seats —
    a 6-seat crew cab is a nice-to-have, so this component is capped at 30
    (was 45) and the roof term in score_small_van carries more weight.

    Marketplace sources (Marktplaats / AutoScout24 / AutoTrack / 2dehands)
    don't expose ``seats`` per listing, so structured field is often None.
    We cascade title-based inference: explicit count ("9-persoons", "2p"),
    crew-cab markers ("DC", "Dubbele Cabine", "Combi") → assume 6 seats,
    factory-passenger trim ("Multivan", "Traveller") → assume 7 seats.
    """
    hay = " ".join(filter(None, [
        vehicle.get("title"), vehicle.get("remarks"),
        vehicle.get("additional_information"),
    ])).lower()
    title = vehicle.get("title") or ""

    seats = vehicle.get("seats")
    if seats is None:
        seats = _seats_from_text(title)
    if seats is None and _CREW_CAB_RE.search(hay):
        seats = 6           # double cab = 5-6 seat factory bench
    if seats is None and _PASSENGER_TRIM_RE.search(title):
        seats = 7           # passenger trim = 7-9 seat factory config

    pts = 0
    if seats is not None:
        if seats >= 6:    pts = 22
        elif seats == 5:  pts = 16
        elif seats == 4:  pts = 10
        elif seats == 3:  pts = 6
        else:             pts = 2

    if re.search(r"\b(anchor\s*point|rail|zitplaats|afneembare?\s*stoel|klapstoel)\b", hay):
        pts = min(pts + 5, 30)

    return min(pts, 30)


def _svs_city_practicality(variant: Optional[str], fuel: Optional[str]) -> int:
    """City practicality — 0-20 pts.  L1 > L2 > L3; petrol/hybrid bonus.

    Whitelist groups are all small/medium L1-L2 vans by construction, so
    unknown size defaults to 14 (between L2=15 and L3=10) rather than 10
    — the soft-gate philosophy: no penalty for missing info on a known
    small-van group.
    """
    s = (variant or "").upper()
    size_pts = 14

    m = re.match(r"L([1-4?])H([1-3?])$", s)
    if m:
        L = m.group(1)
        if L == "1":   size_pts = 18
        elif L == "2": size_pts = 15
        elif L == "3": size_pts = 10
        elif L == "4": size_pts = 4
        else:          size_pts = 14

    fuel_pts = 0
    f = (fuel or "").lower()
    if "hybrid" in f or "electric" in f:
        fuel_pts = 2
    elif "petrol" in f or "benzine" in f:
        fuel_pts = 1

    return min(size_pts + fuel_pts, 20)


def _svs_conversion_potential(vehicle: dict) -> int:
    """Conversion potential — 0-20 pts.  Fold-flat seats, sleeping signals, skylights."""
    hay = " ".join(filter(None, [
        vehicle.get("title"), vehicle.get("remarks"),
        vehicle.get("additional_information"),
    ])).lower()

    pts = 8
    if re.search(r"\b(fold[\s-]?flat|inklapbare?\s*stoel|fold\s*down\s*seat|klapstoel)\b", hay):
        pts = min(pts + 6, 20)
    if re.search(r"\b(sleeping|slaap|bed|matrass|matras|camperklaar|camper\s*ready)\b", hay):
        pts = min(pts + 6, 20)
    if re.search(r"\b(skylight|dakraam|panoramisch?\s*dak|glasdak)\b", hay):
        pts = min(pts + 3, 20)
    if re.search(r"\b(kombi|combi|glazen\s*zij|side\s*window\s*van)\b", hay):
        pts = min(pts + 3, 20)

    return min(pts, 20)


def _svs_value(deal_ratio: Optional[float], vehicle: Optional[dict] = None) -> int:
    """Value points 0-15.

    Auction feed: ``deal_ratio`` is hammer-vs-market — primary signal.
    Asking feed: no hammer exists, so fall through to
    ``price_pct_vs_median`` (the cohort percentile asking_feed computes).
    A listing 15% below cohort median is treated like a deal_ratio of
    ~0.15, etc.
    """
    if deal_ratio is None and vehicle is not None:
        pct = vehicle.get("price_pct_vs_median")
        if pct is not None:
            if pct <= -25:  return 15
            if pct <= -15:  return 12
            if pct <= -5:   return 8
            if pct <=  5:   return 5
            return 2
    if deal_ratio is None:
        return 5
    if deal_ratio >= 0.30:  return 15
    if deal_ratio >= 0.20:  return 12
    if deal_ratio >= 0.10:  return 8
    if deal_ratio >= 0.0:   return 5
    return 2


# Per-group quality factor for camper-base suitability. Applied as a
# multiplicative scalar on the final score, so two otherwise-identical
# vehicles in different groups rank by platform quality. Transit Custom
# is the European benchmark for small-camper conversions (huge aftermarket
# ecosystem, best resale, optimal payload/height). The NV300 platform
# (Vivaro/Trafic/Primastar/Talento) sits ~12% below it — same dimensional
# class but smaller camper-kit ecosystem and lower resale.
_GROUP_QUALITY = {
    "transit_custom_l2h1":         1.00,   # benchmark
    "expert_jumpy_proace_l2":      0.95,   # great platform, slightly thinner aftermarket
    "scudo_gen3":                  0.92,   # rebadged Expert; thin used-market data
    "vivaro_trafic_primastar_l2":  0.88,   # NV300 platform — ~12% below Transit Custom
    "t6_1_lwb":                    1.00,   # T6.1 California ecosystem rivals Transit Custom
    "psa_l1l2h1":                  0.90,   # L3H2 Ducato is the EU camper gold standard; raised from 0.85
    "vito_v_class_l2":             0.95,   # premium chassis but quirky for DIY conversion
    "hyundai_staria":              0.90,   # excellent for passenger use; new platform, rare in NL
    # High-roof panel-van families (L2H2 pivot) — the prime conversion bases.
    "ford_transit_l2h2":           0.95,   # huge aftermarket, the US/UK conversion staple
    "mercedes_sprinter":           1.00,   # the global stand-up campervan benchmark
    "vw_crafter_tge":              0.98,   # gen-2 Crafter / TGE, excellent build
    "renault_master_grp":          0.90,   # Master/Movano/Interstar — cheap, capable base
}


# Title keywords that mark a factory passenger trim (already insulated,
# windows fitted, seats + climate trim installed). These vans need much
# less conversion work to become a camper than their cargo siblings, so
# they earn a flat +PASSENGER_BONUS on the suitability score on top of
# whatever the per-component scoring produced. Detection is OR'd with a
# seats>=7 check — many listings drop the trim name and just say "9p".
_PASSENGER_TRIM_RE = re.compile(
    r"\b("
    r"traveller|spacetourer|space\s*tourer|"          # PSA passenger trio
    r"verso|"                                          # Toyota ProAce Verso
    r"tourer|tourneo|"                                 # Vito Tourer, Ford Tourneo
    r"caravelle|multivan|california|"                  # VW passenger T-line
    r"v-class|v\s*klasse|"                             # Mercedes V-Class
    r"combi|kombi|"                                    # generic passenger config
    r"personenvervoer|personentransporter|"            # NL/DE legal designation
    r"shuttle|bus\s+9p|9\s*persoons"                   # ad-hoc passenger markers
    r")\b",
    re.IGNORECASE,
)
_PASSENGER_BONUS = 15

# High-output engine markers (the user's "Tier A+" signal). 180hp+ is
# uncommon in this class — most listings are 100-150hp. A top engine
# means torque to spare for camper weight, better cruising at autobahn
# speeds, and usually correlates with high-trim Highline / Avantgarde /
# Tourer specs. Match: numeric "180hp"/"180pk"/"180ps" or above, kw form
# "135kw"+ (≈ 180hp), or the explicit "BiTDI" / "Bi-TDI" marker
# (VW's 204hp twin-turbo, T6 only).
_HIGH_POWER_RE = re.compile(
    r"\b(?:"
    r"(?:1[89]\d|[2-9]\d{2})\s*(?:hp|pk|ps|bhp)"   # 180-999 hp/pk/ps/bhp
    r"|(?:1[3-9]\d|[2-9]\d{2})\s*kw"               # 130+ kW ≈ 175+ hp
    r"|bi[-\s]?tdi"                                 # VW BiTDI 204hp
    r")\b",
    re.IGNORECASE,
)
_HIGH_POWER_BONUS = 10

# 4x4 / AWD markers. All-wheel-drive in this van class is rare and
# highly desirable for camper conversion — opens up forest tracks,
# beach access, winter Alpine roads. VW 4Motion (Syncro on T-line),
# Mercedes 4Matic (Vito), Nissan 4WD Trafic Quickshift X-Trail editions
# are the prime targets. Add a small bonus to surface them.
_OFFROAD_RE = re.compile(
    r"\b(?:"
    r"4x4|4\s*motion|4motion|syncro|"               # VW
    r"4matic|4-matic|"                              # Mercedes
    r"awd|all[-\s]wheel[-\s]drive|"                 # generic
    r"quattro|"                                     # Audi (rare in this class)
    r"4wd"
    r")\b",
    re.IGNORECASE,
)
_OFFROAD_BONUS = 10


def _full_haystack(vehicle: dict) -> str:
    """title + remarks + additional_information, joined, lowercased.
    Used for keyword bonuses so listings that put the trim/engine info in
    the description (not the title) don't get penalised."""
    return " ".join(filter(None, [
        vehicle.get("title"), vehicle.get("remarks"),
        vehicle.get("additional_information"),
    ])).lower()


def _passenger_bonus(vehicle: dict) -> int:
    """Flat bonus for factory-passenger trims (Traveller, Caravelle, V-Class…).
    OR'd with seats>=7 since many listings drop the trim name and just say "9p"."""
    seats = vehicle.get("seats") or 0
    if seats >= 7:
        return _PASSENGER_BONUS
    if _PASSENGER_TRIM_RE.search(_full_haystack(vehicle)):
        return _PASSENGER_BONUS
    return 0


def _high_power_bonus(vehicle: dict) -> int:
    """Flat bonus when title or description declares ≥180hp / ≥130kW / BiTDI."""
    return _HIGH_POWER_BONUS if _HIGH_POWER_RE.search(_full_haystack(vehicle)) else 0


def _offroad_bonus(vehicle: dict) -> int:
    """Flat bonus for 4x4 / 4Motion / Syncro / 4Matic / AWD markers."""
    return _OFFROAD_BONUS if _OFFROAD_RE.search(_full_haystack(vehicle)) else 0


def score_small_van(vehicle: dict) -> int:
    """Return a 0-100 camper-candidate suitability score."""
    variant       = vehicle.get("variant") or vehicle.get("van_type")
    fuel          = vehicle.get("fuel")
    deal_ratio    = vehicle.get("deal_ratio")
    group         = vehicle.get("model_group") or ""

    a = _svs_dual_use(vehicle)
    b = _svs_city_practicality(variant, fuel)
    c = _svs_conversion_potential(vehicle)
    d = _svs_value(deal_ratio, vehicle)

    raw = a + b + c + d
    quality = _GROUP_QUALITY.get(group, 0.90)
    base = round(raw * quality)

    # Roof height is the decisive factor post-pivot (June 2026): the camper
    # conversion needs standing height. Reward a CONFIRMED high roof (H2/H3)
    # heavily; penalise a confirmed low roof (H1 = no standing). Unknown
    # height stays neutral (no guess). This swing (≈50 pts) deliberately
    # outweighs the seat bonus so a 2-seat L2H2 cargo van outranks a
    # 6-seat L1H1.
    if variant:
        if "H2" in variant or "H3" in variant:
            base = base + 28
        elif "H1" in variant:
            base = max(0, base - 22)

    # Unknown-size penalty: listings without any explicit L/H code are
    # uncertain — could be any size. Prefer listings that tell you what you're
    # getting. Partial knowledge (one dimension unknown) gets a smaller penalty.
    if variant is None:
        base = max(0, base - 10)
    elif "?" in variant:
        base = max(0, base - 5)

    # VW Transporter L1 (SWB / Caravelle Kurz): useful 6-seat van but
    # shorter living space than L2 (LWB). Penalise slightly vs L2.
    if group == "t6_1_lwb" and variant and variant.startswith("L1"):
        base = max(0, base - 10)

    return min(base + _passenger_bonus(vehicle) + _high_power_bonus(vehicle) + _offroad_bonus(vehicle), 100)


# ---------------------------------------------------------------------------
# ROI scoring (rental-income-first; kept for the secondary ranking)
# ---------------------------------------------------------------------------

_ROI_CAMPER_SIGNALS = re.compile(
    r"\b(bed|slaap|camping|camper|wohnmobil|keuken|kitchen|solar|zonnepaneel"
    r"|mobilhome|motorhome|kampeer|converted|omgebouw)\b",
    re.IGNORECASE,
)

# Liquidity / demand keyed by group rather than legacy token — the
# whitelist groups all rent well in the NL crew-van market. V-Class /
# Staria score slightly lower on liquidity because they're rarer (smaller
# resale pool) but higher on demand because they're factory passenger
# config (no conversion needed).
_ROI_LIQUIDITY = {
    "transit_custom_l2h1":         10,
    "expert_jumpy_proace_l2":       9,
    "scudo_gen3":                   7,   # gen-3 Scudo is new, thin resale market so far
    "vivaro_trafic_primastar_l2":  10,
    "t6_1_lwb":                     9,
    "psa_l1l2h1":                   8,   # Ducato/Boxer/Jumper — common, good resale
    "vito_v_class_l2":              7,   # Mercedes premium tax — slower resale
    "hyundai_staria":               5,   # rare in NL, small resale pool
}

_ROI_DEMAND = {
    "transit_custom_l2h1":          9,
    "expert_jumpy_proace_l2":       8,
    "scudo_gen3":                   7,
    "vivaro_trafic_primastar_l2":   8,
    "t6_1_lwb":                     9,
    "psa_l1l2h1":                   7,   # L1H1/L2H1 crew demand lower than Transit Custom
    "vito_v_class_l2":              9,   # factory crew-cab passenger demand is strong
    "hyundai_staria":               7,
}


def _roi_demand(vehicle: dict) -> float:
    seats = vehicle.get("seats") or 0
    group = vehicle.get("model_group") or ""
    base = _ROI_DEMAND.get(group, 6)
    if seats >= 6:
        base = min(10, base + 2)
    elif seats == 5:
        base = min(10, base + 1)
    title = (vehicle.get("title") or "").lower()
    if re.search(r"\b(dubbele cabine|double cab|crew cab|kombi|dubbelcabine)\b", title):
        base = min(10, base + 1)
    haystack = " ".join(filter(None, [title, vehicle.get("remarks") or "",
                                      vehicle.get("additional_information") or ""]))
    if _ROI_CAMPER_SIGNALS.search(haystack):
        base = max(1, base - 3)
    return float(base)


def _roi_utilization(vehicle: dict) -> float:
    seats = vehicle.get("seats") or 0
    title = (vehicle.get("title") or "").lower()
    if seats >= 6 or re.search(r"\b(dubbele cabine|double cab|crew|kombi)\b", title):
        return 10.0
    # Whitelist groups are all city-friendly small vans
    return 8.0


def _roi_cost_efficiency(vehicle: dict) -> float:
    cost = vehicle.get("final_cost_estimate")
    if cost is None:
        return 5.0
    if cost < 5_000:   return 10.0
    if cost < 7_000:   return 9.0
    if cost < 9_000:   return 8.0
    if cost < 12_000:  return 7.0
    if cost < 16_000:  return 5.5
    if cost < 22_000:  return 4.0
    return 2.5


def _roi_liquidity(vehicle: dict) -> float:
    group = vehicle.get("model_group") or ""
    base = float(_ROI_LIQUIDITY.get(group, 5))
    title = (vehicle.get("title") or "").lower()
    haystack = " ".join(filter(None, [title, vehicle.get("remarks") or "",
                                      vehicle.get("additional_information") or ""]))
    if _ROI_CAMPER_SIGNALS.search(haystack):
        base = max(1.0, base - 3.0)
    return base


def _roi_penalties(vehicle: dict) -> float:
    penalty = 0.0
    haystack = " ".join(filter(None, [
        vehicle.get("title") or "",
        vehicle.get("remarks") or "",
        vehicle.get("additional_information") or "",
    ])).lower()
    camper_hits = len(_ROI_CAMPER_SIGNALS.findall(haystack))
    penalty += min(4.0, camper_hits * 1.0)
    km = vehicle.get("km") or 0
    if km > 200_000: penalty += 1.0
    elif km > 150_000: penalty += 0.5
    emission = str(vehicle.get("emission_standard") or "")
    if re.search(r"\b[34]\b", emission):
        penalty += 0.5
    return penalty


def score_roi(vehicle: dict) -> float:
    demand    = _roi_demand(vehicle)
    util      = _roi_utilization(vehicle)
    cost_eff  = _roi_cost_efficiency(vehicle)
    liquidity = _roi_liquidity(vehicle)
    penalties = _roi_penalties(vehicle)
    raw = (
        0.35 * demand
        + 0.30 * util
        + 0.20 * cost_eff
        + 0.15 * liquidity
        - penalties
    )
    return round(max(0.0, min(10.0, raw)), 2)


def roi_tier(score: float) -> str:
    if score >= 8.5: return "S"
    if score >= 7.0: return "A"
    if score >= 5.5: return "B"
    return "C"


# ---------------------------------------------------------------------------
# Reason-string builder for human-readable evaluation output
# ---------------------------------------------------------------------------

def _build_reasons(
    group: str,
    variant: Optional[str],
    classification_evidence: Optional[List[str]],
    km: Optional[int],
    year: Optional[int],
) -> List[str]:
    label = WHITELIST_GROUPS[group]["label"]
    out = [f"{label} ({variant or 'size unknown'})"]
    if classification_evidence:
        out.append("; ".join(classification_evidence))
    if km is not None or year is not None:
        km_s = f"{km // 1000}k km" if km else "km ?"
        yr_s = str(year) if year else "year ?"
        out.append(f"{km_s}, {yr_s}")
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
    weight_kg = getattr(vehicle, "weight_kg", None)
    body_type = getattr(vehicle, "body_type", None)

    haystack = " ".join(s for s in [title, model_a, remarks, addl] if s)
    description = " ".join(s for s in [model_a, remarks, addl] if s)

    # ── Stage 1: Hard filters (vehicle type / body / damage / fuel / km) ─
    r = _check_list(haystack, HARD_REJECT_TYPE)
    if r:
        return Evaluation(False, f"vehicle_type: {r}", None, None, None, None, None, None, None)

    r = _check_list(haystack, HARD_REJECT_BODY)
    if r:
        return Evaluation(False, f"body_mismatch: {r}", None, None, None, None, None, None, None)

    r = _check_list(haystack, HARD_REJECT_DAMAGE)
    if r:
        return Evaluation(False, f"damage: {r}", None, None, None, None, None, None, None)

    # Fuel is informational — no hard reject. Electric / diesel / petrol
    # / hybrid / CNG / LPG / hydrogen all pass through. Range and
    # refueling are buyer-side concerns, not classifier concerns.

    if km is not None and km > 250_000:
        return Evaluation(False, f"mileage_too_high: {km}km", None, None, None, None, None, None, None)

    # ── Stage 2: Classification (hard gate on whitelist) ─────────────────
    cls = classify_vehicle(title, description, weight_kg=weight_kg, body_type=body_type)
    if cls.group is None:
        # Distinguish smaller-sibling rejects for clarity
        sib_ev = (cls.evidence or [None])[0]
        if sib_ev and sib_ev.startswith("smaller_sibling"):
            return Evaluation(False, sib_ev, None, None, None, None, None, None, None)
        return Evaluation(False, "brand_not_in_whitelist", None, None, None, None, None, None, None)

    # ── Stage 3: Strict filter (soft gate on year / Euro / seats / size) ─
    passed, reason = strict_filter(vehicle, cls)
    if not passed:
        return Evaluation(
            False, reason, cls.variant,
            cls.group, cls.variant, cls.confidence,
            None, cls.group, None,
        )

    # ── Stage 4: Scoring (small-van suitability) ─────────────────────────
    # Convert the Pydantic-style vehicle to a dict view for the scorers.
    v_dict = {
        "title":  title,
        "remarks": remarks,
        "additional_information": addl,
        "seats":  getattr(vehicle, "seats", None),
        "fuel":   fuel,
        "km":     km,
        "year":   year,
        "emission_standard": getattr(vehicle, "emission_standard", None),
        "variant": cls.variant,
        "model_group": cls.group,
        "matched_token": cls.matched_token,
        "deal_ratio": getattr(vehicle, "deal_ratio", None),
        "final_cost_estimate": getattr(vehicle, "final_cost_estimate", None),
    }
    score = score_small_van(v_dict)

    reasons = _build_reasons(cls.group, cls.variant, cls.evidence, km, year)

    if score < SCORE_THRESHOLD:
        return Evaluation(
            True, f"score_below_threshold: {score} < {SCORE_THRESHOLD}",
            cls.variant, cls.group, cls.variant, cls.confidence,
            score, cls.group, reasons,
        )

    return Evaluation(
        True, None, cls.variant,
        cls.group, cls.variant, cls.confidence,
        score, cls.group, reasons,
    )

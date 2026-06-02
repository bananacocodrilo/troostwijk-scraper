"""Risk scoring for camper-candidate lots.

Adds a 0-5 risk score plus a list of risk flags per vehicle. This is
purely additive metadata — it does NOT change the existing hard filters
or classification. Catastrophic stuff (damage in title, no MOT, etc.)
already rejects upstream; this module surfaces *soft* risk signals
that matter at the decision stage but currently slip through:

  * ex-government / municipal fleet (often beat up)
  * missing inspection certificate noted in remarks
  * unverified mileage notes (no CarPass / tellerstand onlogisch)
  * high km-per-year ratio (highway-thrashed)
  * very high total km (transmission risk on passenger trims like
    V-Class / Multivan that have sensitive DSG/9G boxes)
  * damage hints in remarks (not in title — title-damage already
    hard-filtered)
  * unclear VAT scheme (informational only — no score bump)

The risk score is summed across triggered flags and capped at 5.
Conservative regex throughout: prefer false negative over false
positive (it's better to under-flag than mis-label a clean lot).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Whitelist groups whose default buyer profile is a premium passenger
# trim with a sensitive automatic gearbox (V-Class 9G-Tronic, Hyundai
# Staria 8AT). These transmissions tend to give out around 150-180k.
# Cargo siblings have simpler manuals or torque-converter autos that
# happily clock 250k+, so they get the higher 200k threshold.
#
# Other whitelist groups (transit_custom_l2h1, expert_jumpy_proace_l2,
# vivaro_trafic_primastar_l2, t6_1_lwb, scudo_gen3, psa_l1l2h1) cover
# BOTH cargo and passenger trims in one group. For those, we promote
# to the 150k threshold only when the lot text actually flags a
# passenger trim (Multivan, Caravelle, Traveller, SpaceTourer, Verso,
# Tourneo, V-Class…) — see _PASSENGER_TRIM_RE / _is_passenger below.
_PASSENGER_GROUPS: set = {
    "hyundai_staria",          # always passenger MPV
    "vito_v_class_l2",         # V-Class trim is premium-auto territory
}

# Plain-text fingerprints inside the lot text (title + remarks + extras)
# indicating a factory passenger trim. Same list as van_intel uses for
# the +15 scoring bonus — reused here so risk + scoring agree on
# what "passenger" means. Kept local (small + stable) to avoid a
# circular import with van_intel.
_PASSENGER_TRIM_RE = re.compile(
    r"\b("
    r"traveller|spacetourer|space\s*tourer|"
    r"verso|"
    r"tourer|tourneo|"
    r"caravelle|multivan|california|"
    r"v-class|v\s*klasse|"
    r"combi|kombi|"
    r"personenvervoer|personentransporter|"
    r"shuttle"
    r")\b",
    re.IGNORECASE,
)

# ──────────────────────────────────────────────────────────────────────────
# Regex patterns for each flag. All use \b word boundaries to avoid false
# positives ("government" matching "self-governmentality" style noise).
# ──────────────────────────────────────────────────────────────────────────

# Government / municipal fleet markers. NL "gemeente", DE "ministerie",
# generic English "ex-gov" / "government vehicle". Captures both the
# hyphenated and space-separated forms.
_EX_GOV_RE = re.compile(
    r"\b("
    r"ex[-\s]?government|ex[-\s]?gov|"
    r"government\s+vehicle|"
    r"gemeente|gemeentelijk|"
    r"ministerie|ministry"
    r")\b",
    re.IGNORECASE,
)

# Missing inspection certificate ("APK" in NL, "Keuring" in BE,
# "contrôle technique" in FR). Phrased negatively — the lot is
# explicitly flagging that no cert exists.
_NO_INSPECTION_RE = re.compile(
    r"\b("
    r"no\s+inspection\s+certificate|"
    r"geen\s+apk|"
    r"geen\s+keuring|"
    r"sans\s+contr[oô]le\s+technique|"
    r"keine\s+t[uü]v"
    r")\b",
    re.IGNORECASE,
)

# Unverified mileage / no CarPass certification. CarPass is Belgium's
# odometer-fraud-protection scheme; missing it is a yellow flag in NL/BE.
# "tellerstand onlogisch" = "illogical odometer reading" (NL CBS code).
_UNVERIFIED_KM_RE = re.compile(
    r"\b("
    r"km\s*not\s*verified|"
    r"niet\s*verifieerbaar|"
    r"tellerstand\s*onlogisch|"
    r"no\s+carpass|geen\s+carpass|"
    r"km\s*unknown|km\s*onbekend"
    r")\b",
    re.IGNORECASE,
)

# Damage hints in REMARKS only (title-damage rejects upstream via
# van_intel hard filters). Negative lookbehind avoids "no damage" /
# "geen schade" false positives. We do NOT include "kras" alone in
# the lookbehind exclusion because "geen krassen" is uncommon, but
# we accept that as a tolerable false-positive rate.
_DAMAGE_REMARKS_RE = re.compile(
    r"(?<!no\s)(?<!geen\s)(?<!kein\s)(?<!sans\s)"
    r"\b("
    r"schade|damage[ds]?|beschadig\w*|"
    r"dent|deuk|"
    r"scratch(?:es|ed)?|kras(?:sen)?"
    r")\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _haystack(v: dict, *, include_title: bool = True) -> str:
    """Concatenate the free-text fields we scan. Title can be excluded for
    damage detection (title-damage already hard-filters, so we only want
    to flag damage notes that slipped through into the description)."""
    parts: List[str] = []
    if include_title:
        parts.append(str(v.get("title") or ""))
    parts.append(str(v.get("remarks") or ""))
    parts.append(str(v.get("additional_information") or ""))
    return " ".join(parts)


def _year_age(year: Optional[int]) -> Optional[int]:
    """Vehicle age in years (≥1). None when year is unknown."""
    if year is None:
        return None
    try:
        y = int(year)
    except (TypeError, ValueError):
        return None
    age = date.today().year - y
    return max(age, 1)


def _is_passenger(v: dict) -> bool:
    """True when the lot looks like a factory passenger trim. Uses
    explicit group + seats heuristics + the passenger-trim regex.
    Stays local (no van_intel import) to keep this module dependency-free."""
    group = v.get("model_group")
    if group in _PASSENGER_GROUPS:
        return True
    seats = v.get("seats")
    if seats is not None and isinstance(seats, (int, float)) and seats >= 7:
        return True
    if _PASSENGER_TRIM_RE.search(_haystack(v)):
        return True
    return False


def _flag(code: str, label: str, severity: str) -> dict:
    return {"code": code, "label": label, "severity": severity}


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------

def compute_risk(v: dict) -> dict:
    """Return ``{risk_score: 0-5, risk_flags: [...]}`` for a vehicle dict.

    Each triggered flag contributes to the score (info=0, warn=+1, high=+2).
    The km-per-year flag is mutually exclusive: ``very_high_km_per_year``
    replaces ``high_km_per_year`` rather than stacking with it. The final
    score is capped at 5.
    """
    flags: List[dict] = []
    score = 0

    full_text = _haystack(v, include_title=True)
    remarks_text = _haystack(v, include_title=False)

    # ── ex-government / municipal fleet ────────────────────────────
    if _EX_GOV_RE.search(full_text):
        flags.append(_flag(
            "ex_government",
            "Ex-government / municipal fleet — often heavily used",
            "warn",
        ))
        score += 1

    # ── missing inspection certificate ─────────────────────────────
    if _NO_INSPECTION_RE.search(full_text):
        flags.append(_flag(
            "no_inspection_cert",
            "No inspection certificate (APK / TÜV / contrôle technique)",
            "warn",
        ))
        score += 1

    # ── unverified km ──────────────────────────────────────────────
    if _UNVERIFIED_KM_RE.search(full_text):
        flags.append(_flag(
            "unverified_km",
            "Mileage not verified (no CarPass / odometer flagged illogical)",
            "warn",
        ))
        score += 1

    # ── km / year heuristic ────────────────────────────────────────
    # Skip silently when either side is unknown — soft on missing data.
    km = v.get("km")
    age = _year_age(v.get("year"))
    if km is not None and age is not None and age >= 1:
        try:
            kpy = float(km) / float(age)
        except (TypeError, ValueError, ZeroDivisionError):
            kpy = None
        if kpy is not None:
            if kpy > 30000:
                # high severity — replaces (does NOT add to) the warn-level
                # high_km_per_year flag, per task spec.
                flags.append(_flag(
                    "very_high_km_per_year",
                    f"Very high km/year ({int(kpy):,}/yr) — highway-thrashed risk",
                    "high",
                ))
                score += 2
            elif kpy > 20000:
                flags.append(_flag(
                    "high_km_per_year",
                    f"High km/year ({int(kpy):,}/yr) — above-average wear",
                    "warn",
                ))
                score += 1

    # ── high total km ──────────────────────────────────────────────
    # Passenger trims (V-Class, Multivan, etc.) get the tighter 150k
    # threshold because their auto boxes start failing earlier.
    if km is not None:
        try:
            km_int = int(km)
        except (TypeError, ValueError):
            km_int = None
        if km_int is not None:
            cap = 150000 if _is_passenger(v) else 200000
            if km_int > cap:
                flags.append(_flag(
                    "high_total_km",
                    f"High total km ({km_int:,}) — transmission/engine wear risk",
                    "warn",
                ))
                score += 1

    # ── damage notes in body (not title) ───────────────────────────
    if _DAMAGE_REMARKS_RE.search(remarks_text):
        flags.append(_flag(
            "damage_notes_present",
            "Damage / scratch hints in description — inspect photos closely",
            "warn",
        ))
        score += 1

    # ── unclear VAT scheme (informational; no score) ───────────────
    # We use vat_margin (project's actual field name; spec calls it
    # vat_scheme). vat_margin is True/False once known; None means the
    # scraper couldn't determine recoverability. Only flag when there's
    # a meaningful cost figure attached (otherwise it's pure noise on
    # data-missing lots that haven't been costed yet).
    if v.get("vat_margin") is None and v.get("total_cost_eur") is not None:
        flags.append(_flag(
            "vat_unclear",
            "VAT scheme unclear — recoverability unknown",
            "info",
        ))
        # No score contribution — informational only.

    if score > 5:
        score = 5

    return {"risk_score": score, "risk_flags": flags}

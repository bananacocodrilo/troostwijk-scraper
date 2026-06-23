"""VAT-scheme detection for asking-price listings.

The asking feed pools prices of mixed VAT basis: marketplaces usually show an
incl-VAT or margin-scheme price (what a private buyer pays), while the NL
financial-lease sources quote an **ex-VAT** purchase price — so a lease van looks
~21% cheaper than it really is to a private, non-deductible buyer.

``detect_vat(listing)`` classifies each listing's scheme and computes a comparable
``price_gross_eur`` (what a private buyer actually pays). This is *labelling only* —
the asking-feed cohort medians stay on the raw ``price_eur``; the dashboards expose a
toggle to switch the displayed prices to the gross basis.

Policy (confirmed June 2026):
  * Fixed 21% VAT for all markets (DE is really 19%, but we use 21% by request).
  * Aggressive on ambiguity: reclaimable-VAT wording ("btw verrekenbaar" /
    "MwSt ausweisbar" / "TVA déductible") is treated as ex-VAT → grossed up.
  * Pure-unknown is NOT guessed — left as displayed (assumed incl).

Detection layers, in precedence order: explicit margin → explicit excl → explicit
incl → ambiguous reclaimable → scraper structured hint → lease source default →
unknown. Conservative regex (``\\b`` boundaries) throughout.
"""

import re
from typing import Optional

VAT_RATE = 0.21  # flat, all markets (per project decision)

# Lease sources quote the ex-VAT upfront purchase price (commercial B2B basis).
_LEASE_SOURCES = {"regeljelease", "financiallease", "rosfinance"}

# ---------------------------------------------------------------------------
# Text patterns (lowercased haystack = title + remarks/description)
# ---------------------------------------------------------------------------

# Margin scheme — no VAT line, price is final for everyone (no reclaim).
_RE_MARGIN = re.compile(
    r"\bmarge\b|margeregeling|marge\s*regeling|margin\s*scheme"
    r"|differenzbesteuer\w*|\bmargenbesteuerung\b|§\s*25a",
    re.IGNORECASE,
)

# Negated margin ("geen marge" / "zonder marge" / "keine marge") — NOT a margin lot.
_RE_MARGIN_NEG = re.compile(r"\b(?:geen|zonder|kein\w*|no)\s+marge", re.IGNORECASE)

# Explicit VAT-EXCLUSIVE — the displayed number is net; a private buyer adds VAT.
_RE_EXCL = re.compile(
    r"\bexcl\.?\s*btw\b|\bexclusief\s*btw\b|\bex\.?\s*btw\b|\+\s*btw\b|\bbtw\s*erbij\b"
    r"|\bnetto\b|\bnetto\s*preis\b|\bzz?gl\.?\s*mwst\b|\bzuz(?:ü|ue)gl\w*\s*mwst\b"
    r"|\bexkl\.?\s*mwst\b|\bhors\s*tva\b|\btva\s*non\s*comprise\b",
    re.IGNORECASE,
)

# Explicit VAT-INCLUSIVE — the displayed number already includes VAT.
_RE_INCL = re.compile(
    r"\bincl\.?\s*btw\b|\binclusief\s*btw\b|\bbtw\s*inbegrepen\b"
    r"|\binkl?\.?\s*mwst\b|\bincl\.?\s*mwst\b|\bbtw\s*inclusief\b"
    r"|\btva\s*comprise\b|\bttc\b",
    re.IGNORECASE,
)

# Ambiguous reclaimable-VAT — VAT applies and a business can reclaim it, but the
# wording doesn't state whether the displayed number is incl or excl. Per the
# aggressive policy we treat these as ex-VAT (grossed up).
_RE_DEDUCTIBLE = re.compile(
    r"\bbtw\s*verrekenbaar\b|\bbtw\s*aftrekbaar\b|\bbtw\s*verlegd\b|\bbtw[-\s]?auto\b"
    r"|\bbtw\s*wagen\b|\bmwst\.?\s*ausweisbar\b|\bausweisbare?\s*mwst\b"
    r"|\btva\s*d(?:é|e)ductible\b",
    re.IGNORECASE,
)


def _haystack(listing: dict) -> str:
    return " ".join(str(listing.get(k) or "") for k in ("title", "remarks", "description"))


def hint_from_text(text: Optional[str]) -> Optional[str]:
    """Map a structured VAT attribute value (e.g. an Adevinta "btw" attribute
    like "Marge" / "Inclusief BTW" / "Exclusief BTW") to a scheme, or None.
    Used by scrapers to populate ``listing["vat_hint"]`` defensively."""
    if not text:
        return None
    s = str(text)
    if _RE_MARGIN.search(s) and not _RE_MARGIN_NEG.search(s):
        return "margin"
    if _RE_EXCL.search(s):
        return "excl"
    if _RE_INCL.search(s):
        return "incl"
    if _RE_DEDUCTIBLE.search(s):
        return "vat_deductible"
    return None


# Schemes that mean "private buyer pays VAT on top of the displayed price".
_GROSS_UP = {"excl", "vat_deductible"}


def detect_vat(listing: dict) -> dict:
    """Return VAT fields to merge into a listing dict:

    ``vat_scheme``       one of margin | incl | excl | vat_deductible | unknown
    ``price_gross_eur``  what a private (non-deductible) buyer pays (21% flat)
    ``vat_evidence``     short string explaining the classification
    ``vat_confidence``   high | medium | low
    """
    price = listing.get("price_eur")
    source = (listing.get("source") or "").lower()
    hay = _haystack(listing)

    scheme: Optional[str] = None
    evidence = ""
    confidence = "low"

    # 1-4: explicit text signals (highest precedence).
    if _RE_MARGIN.search(hay) and not _RE_MARGIN_NEG.search(hay):
        scheme, evidence, confidence = "margin", "text: marge/margin scheme", "high"
    elif _RE_EXCL.search(hay):
        scheme, evidence, confidence = "excl", "text: excl. btw / netto / zzgl. mwst", "high"
    elif _RE_INCL.search(hay):
        scheme, evidence, confidence = "incl", "text: incl. btw / inkl. mwst", "high"
    elif _RE_DEDUCTIBLE.search(hay):
        # Aggressive: reclaimable → treat as ex-VAT.
        scheme, evidence, confidence = "vat_deductible", "text: btw verrekenbaar / mwst ausweisbar (treated ex-VAT)", "medium"

    # 5: scraper-provided structured hint (incl/excl/margin/vat_deductible).
    if scheme is None:
        hint = listing.get("vat_hint")
        if hint in ("margin", "incl", "excl", "vat_deductible"):
            scheme, evidence, confidence = hint, f"structured: {hint}", "high"

    # 6: source default — lease quotes ex-VAT.
    if scheme is None and source in _LEASE_SOURCES:
        scheme, evidence, confidence = "excl", "source default: lease quotes ex-VAT", "medium"

    # 7: unknown — don't guess; treat displayed price as final.
    if scheme is None:
        scheme, evidence, confidence = "unknown", "no VAT signal", "low"

    if isinstance(price, (int, float)):
        gross = round(price * (1 + VAT_RATE)) if scheme in _GROSS_UP else round(price)
    else:
        gross = None

    return {
        "vat_scheme": scheme,
        "price_gross_eur": gross,
        "vat_evidence": evidence,
        "vat_confidence": confidence,
    }

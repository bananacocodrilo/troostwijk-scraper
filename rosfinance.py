"""Rosfinance.nl (NL) asking-price index — best-effort.

Rosfinance.nl renders its occasion inventory client-side (list.js SPA): the
listing HTML carries no server-side vehicle data, the sitemap exposes no
occasion URLs, and the JSON inventory endpoint (``/api/occasions``) returns
401 Unauthorized without a session token. From a plain server-side fetch
there is therefore nothing to parse.

This module is kept for parity and forward-compatibility: it attempts a
lightweight scrape and degrades cleanly to ``[]`` (never raises), so the
pipeline runs unaffected. If a public/authorised endpoint becomes available,
wire it into ``fetch_market_prices``.

Listing dict (when populated): price_eur, year, km, title, url, source, images, model_key, country
"""

from typing import List, Optional

SOURCE = "rosfinance"


def fetch_market_prices(model_key: str, pages: int = 1) -> List[dict]:
    # No server-side data available (JS SPA + authenticated API). See module docstring.
    return []


def build_listings(
    model_keys: Optional[List[str]] = None,
    pages_per_model: int = 1,
) -> List[dict]:
    print("  rosfinance: 0 listings (JS SPA; inventory API requires auth — "
          "no server-side data to scrape)")
    return []


if __name__ == "__main__":
    print("total:", len(build_listings()))

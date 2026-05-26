import re

VAN_KEYWORDS = [
    "boxer",
    "ducato",
    "jumper",
    "transit",
    "sprinter",
    "master",
    "crafter",
    "movano",
]

SIZE_PATTERN = re.compile(r"\bL([1-4])\s*H([1-3])\b", re.IGNORECASE)


def is_valid_van(title: str) -> bool:
    if not title:
        return False
    t = title.lower()
    return any(k in t for k in VAN_KEYWORDS)


def detect_size(text: str) -> str | None:
    if not text:
        return None
    m = SIZE_PATTERN.search(text)
    if not m:
        return None
    return f"L{m.group(1)}H{m.group(2)}".upper()


def score_vehicle(year, km, van_type, fuel) -> int:
    score = 0

    if year is not None:
        if year >= 2020:
            score += 30
        elif year >= 2015:
            score += 20
        else:
            score += 10

    if km is not None:
        if km < 100_000:
            score += 30
        elif km <= 180_000:
            score += 20
        else:
            score += 5

    if van_type in ("L3H2", "L4H3"):
        score += 20
    elif van_type == "L2H2":
        score += 15
    else:
        score += 5

    if fuel == "diesel":
        score += 10

    return min(score, 100)


def is_hidden_gem(score: int, year, km) -> bool:
    if score < 75:
        return False
    if year is None or year < 2017:
        return False
    if km is None or km > 150_000:
        return False
    return True

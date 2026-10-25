from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from datetime import UTC, date, datetime
from typing import Any

_WS_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^a-z0-9%.\-]+")
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.\-][a-z0-9]+)*")


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utcnow().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def json_loads(value: str | bytes | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalise_text(text: str) -> str:
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2014", "-").replace("\u2013", "-")
    return _WS_RE.sub(" ", text).strip()


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(normalise_text(text).lower())


_STOP = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are", "was", "were",
    "be", "been", "being", "by", "with", "as", "at", "from", "that", "this", "which", "who",
    "shall", "may", "under", "not", "no", "any", "such", "its", "it", "his", "her", "he",
    "she", "they", "their", "has", "have", "had", "if", "where", "when", "all", "also",
    "into", "than", "then", "there", "these", "those", "but", "so", "up", "out", "about",
    "compliance", "advisory", "question", "employee", "employee's", "employees", "employer",
}

NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100, "thousand": 1000, "lakh": 100000,
}
_DIGIT_RE = re.compile(r"\d[\d,]*")


def numbers_in(text: str) -> set[int]:
    """Extract canonical numbers from digits and English number words.

    Both forms occur in the corpus ("240 days" in a headnote, "two hundred and
    forty days" in the body), so comparing only digits produces false failures.
    Handles compounds such as twenty-six, three thousand five hundred, and two
    hundred and forty.
    """
    values: set[int] = set()
    normalised = text.lower().replace("₹", "").replace(",", "")
    for match in _DIGIT_RE.findall(normalised):
        try:
            values.add(int(match))
        except ValueError:
            continue
    tokens = re.findall(r"[a-z]+", normalised)
    total = 0
    current = 0
    seen = False

    def flush() -> None:
        nonlocal total, current, seen
        if seen:
            values.add(total + current)
        total, current, seen = 0, 0, False

    for token in tokens:
        if token in NUMBER_WORDS:
            seen = True
            word = NUMBER_WORDS[token]
            if word == 100:
                current = max(1, current) * 100
            elif word == 1000:
                total += max(1, current) * 1000
                current = 0
            elif word == 100000:
                total += max(1, current) * 100000
                current = 0
            else:
                current += word
        elif token == "and" and seen:
            continue
        else:
            flush()
    flush()
    return values


def _stem(token: str) -> str:
    """Light, deterministic morphological normalisation.

    The verifier compares statute language ("days", "worked") with a draft's
    paraphrase ("day", "work"); a full stemmer would add a dependency for a
    narrow gain, so only regular English suffixes are removed.
    """
    if len(token) <= 3 or token.isdigit():
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("ss"):
        return token
    if token.endswith("s"):
        token = token[:-1]
    for suffix in ("ing", "ed"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            token = token[: -len(suffix)]
            break
    if token.endswith("ly") and len(token) > 5:
        token = token[:-2]
    return token


def match_tokens(text: str) -> list[str]:
    """Tokens for comparing a claim with a passage.

    Numbers are canonicalised (``two hundred and forty`` and ``240`` both become
    ``#240``) and regular suffixes are removed, so a paraphrase is not failed
    merely because it uses a different grammatical form.
    """
    normalised = normalise_text(text).lower().replace(",", "")
    raw = _TOKEN_RE.findall(normalised)
    result: list[str] = []
    index = 0
    while index < len(raw):
        token = raw[index]
        if token in NUMBER_WORDS:
            total = 0
            current = 0
            seen = False
            while index < len(raw):
                part = raw[index]
                if part in NUMBER_WORDS:
                    seen = True
                    word = NUMBER_WORDS[part]
                    if word == 100:
                        current = max(1, current) * 100
                    elif word == 1000:
                        total += max(1, current) * 1000
                        current = 0
                    elif word == 100000:
                        total += max(1, current) * 100000
                        current = 0
                    else:
                        current += word
                    index += 1
                    continue
                if part == "and" and seen:
                    index += 1
                    continue
                break
            result.append(f"#{total + current}")
            continue
        if token.isdigit():
            result.append(f"#{int(token)}")
            index += 1
            continue
        stemmed = _stem(token)
        if token not in _STOP and stemmed not in _STOP:
            result.append(stemmed)
        index += 1
    return result


def content_tokens(text: str) -> list[str]:
    return [t for t in tokenize(text) if t not in _STOP and len(t) > 1]



def truncate(text: str, limit: int = 600) -> str:
    text = normalise_text(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value)[:10])


def epoch_ms() -> int:
    return int(time.time() * 1000)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((p / 100.0) * (len(ordered) - 1)))))
    return ordered[k]

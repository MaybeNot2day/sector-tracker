"""Deterministic semantic clustering for the live Telegram news feed."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any, cast

# Feed language is mixed EN/RU; keep a shared stoplist so labels describe
# events, not connector words. URL tokens are dropped before stopwording.
STOP_WORDS = {
    "and",
    "about",
    "after",
    "again",
    "against",
    "alert",
    "amid",
    "another",
    "around",
    "back",
    "before",
    "breaking",
    "could",
    "data",
    "during",
    "are",
    "earlier",
    "either",
    "at",
    "from",
    "have",
    "into",
    "just",
    "latest",
    "later",
    "by",
    "market",
    "markets",
    "more",
    "most",
    "near",
    "new",
    "news",
    "now",
    "off",
    "for",
    "once",
    "other",
    "has",
    "had",
    "over",
    "report",
    "reports",
    "said",
    "in",
    "is",
    "says",
    "since",
    "still",
    "than",
    "that",
    "their",
    "there",
    "these",
    "they",
    "of",
    "on",
    "or",
    "this",
    "those",
    "today",
    "under",
    "update",
    "video",
    "while",
    "will",
    "with",
    "without",
    "your",
    "было",
    "будет",
    "будут",
    "был",
    "the",
    "была",
    "все",
    "время",
    "для",
    "если",
    "есть",
    "как",
    "какие",
    "to",
    "которые",
    "над",
    "нам",
    "наши",
    "после",
    "was",
    "were",
    "при",
    "про",
    "сам",
    "свои",
    "себя",
    "сказал",
    "теперь",
    "того",
    "только",
    "уже",
    "что",
    "чтобы",
    "это",
}

LABEL_STOP_WORDS = STOP_WORDS | {
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
}
LABEL_ACRONYMS = {
    "ai",
    "boj",
    "btc",
    "cpi",
    "ecb",
    "eth",
    "etf",
    "fed",
    "fomc",
    "gdp",
    "opec",
    "ppi",
    "uk",
    "us",
}

_TOKEN_RE = re.compile(r"(?:[A-Za-zА-Яа-яЁё]{2,}|\d+(?:[.,]\d+)?%?|\$[A-Z]{1,10})")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def cluster_news(items: list[dict[str, Any]]) -> dict[str, object]:
    """Cluster latest feed items and return treemap-ready groups."""
    docs: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        tokens = _tokens(str(item.get("text") or ""))
        if len(tokens) < 2:
            continue
        docs.append(
            {
                "index": index,
                "item": item,
                "tokens": tokens,
                "tfidf": {},
                "token_set": set(tokens),
            }
        )
    total = len(docs)
    if not total:
        return {"clusters": [], "singletons": 0, "generated_at": _now_iso()}

    document_frequency: Counter[str] = Counter()
    for doc in docs:
        document_frequency.update(doc["token_set"])
    for doc in docs:
        doc["tfidf"] = _tfidf(doc["tokens"], document_frequency, total)

    clusters = _connected_clusters(docs)
    payload_clusters: list[dict[str, object]] = []
    for cluster in clusters:
        member_ids = [
            str(doc["item"].get("id") or "")
            for doc in sorted(
                cluster,
                key=lambda doc: (
                    _timestamp_seconds(doc["item"]),
                    str(doc["item"].get("id") or ""),
                ),
                reverse=True,
            )
        ]
        first_seen = min(
            (stamp for doc in cluster if (stamp := _timestamp_seconds(doc["item"])) is not None),
            default=None,
        )
        latest = max(
            (stamp for doc in cluster if (stamp := _timestamp_seconds(doc["item"])) is not None),
            default=None,
        )
        payload_clusters.append(
            {
                "id": _cluster_id(cluster),
                "label": _cluster_label(cluster),
                "item_ids": member_ids,
                "count": len(cluster),
                "channels": len({str(doc["item"].get("channel") or "") for doc in cluster}),
                "first_seen": _iso_from_seconds(first_seen),
                "latest_seen": _iso_from_seconds(latest),
                "sample": str(cluster[0]["item"].get("text") or ""),
            }
        )
    payload_clusters.sort(key=lambda row: (-cast(int, row["count"]), str(row["id"])))
    return {
        "clusters": payload_clusters,
        "singletons": sum(1 for cluster in payload_clusters if cluster["count"] == 1),
        "generated_at": _now_iso(),
    }


def cluster_revision(items: list[dict[str, Any]]) -> str:
    """Stable cache key: text/timestamp/link shape, not object identity."""
    parts = [str(len(items))]
    for item in items:
        parts.extend(
            [
                str(item.get("id") or ""),
                str(item.get("channel") or ""),
                str(item.get("channel_title") or ""),
                str(item.get("timestamp") or ""),
                str(item.get("text") or ""),
                str(item.get("link") or ""),
            ]
        )
    joined = "\x1f".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _tokens(text: str) -> list[str]:
    normalized = text.lower()
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(normalized):
        token = raw.strip("$.,%")
        if len(token) < 2 or token in STOP_WORDS:
            continue
        tokens.append(token)
    return tokens


def _tfidf(
    tokens: list[str], document_frequency: Counter[str], total_documents: int
) -> dict[str, float]:
    counts = Counter(tokens)
    return {
        token: (count / max(len(tokens), 1))
        * math.log((1 + total_documents) / (1 + document_frequency[token]))
        for token, count in counts.items()
    }


def _similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_tokens = left["token_set"]
    right_tokens = right["token_set"]
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = left_tokens & right_tokens
    if len(overlap) / min(len(left_tokens), len(right_tokens)) < 0.34:
        return 0.0
    left_vector = cast(dict[str, float], left["tfidf"])
    right_vector = cast(dict[str, float], right["tfidf"])
    left_norm = math.sqrt(sum(value * value for value in left_vector.values()))
    right_norm = math.sqrt(sum(value * value for value in right_vector.values()))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(left_vector[token] * right_vector[token] for token in overlap) / (
        left_norm * right_norm
    )


def _connected_clusters(docs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    # Dynamic threshold: short snippets need a high bar; richer text has enough
    # signal to link at lower cosine. The overlap prefilter avoids tiny shared
    # lexicons faking similarity.
    visited: set[int] = set()
    clusters: list[list[dict[str, Any]]] = []
    for start in range(len(docs)):
        if start in visited:
            continue
        stack = [start]
        visited.add(start)
        cluster: list[dict[str, Any]] = []
        while stack:
            current_index = stack.pop()
            current = docs[current_index]
            cluster.append(current)
            for candidate_index, candidate in enumerate(docs):
                if candidate_index in visited:
                    continue
                threshold = 0.48 if len(current["tokens"]) < 10 else 0.42
                if _similarity(current, candidate) >= threshold:
                    visited.add(candidate_index)
                    stack.append(candidate_index)
        clusters.append(cluster)
    return clusters


def _cluster_label(cluster: list[dict[str, Any]]) -> str:
    if len(cluster) == 1:
        return _headline_label(cast(list[str], cluster[0]["tokens"]))
    support: Counter[str] = Counter()
    for doc in cluster:
        support.update(cast(set[str], doc["token_set"]))
    minimum_support = max(2, math.ceil(len(cluster) * 0.34))
    shared_set = {
        token
        for token, count in support.items()
        if count >= minimum_support and _label_token(token)
    }
    shared: list[str] = []
    for token in cast(list[str], cluster[0]["tokens"]):
        if token in shared_set and token not in shared:
            shared.append(token)
    if shared:
        return " / ".join(_display_token(token) for token in shared[:2])
    return _headline_label(cast(list[str], cluster[0]["tokens"]))


def _headline_label(tokens: list[str]) -> str:
    selected = [_display_token(token) for token in tokens if _label_token(token)][:5]
    label = " ".join(selected) or "Market News"
    return label if len(label) <= 54 else f"{label[:51].rstrip()}…"


def _label_token(token: str) -> bool:
    return token not in LABEL_STOP_WORDS and not token[0].isdigit()


def _display_token(token: str) -> str:
    return token.upper() if token in LABEL_ACRONYMS else token.title()


def _cluster_id(cluster: list[dict[str, Any]]) -> str:
    ids = sorted(str(doc["item"].get("id") or "") for doc in cluster)
    digest = hashlib.sha256("\x1f".join(ids).encode("utf-8")).hexdigest()
    return digest[:12]


def _timestamp_seconds(item: dict[str, Any]) -> float | None:
    try:
        return datetime.fromisoformat(str(item.get("timestamp") or "")).timestamp()
    except ValueError:
        return None


def _iso_from_seconds(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC).isoformat()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()

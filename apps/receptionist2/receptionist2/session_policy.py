from __future__ import annotations

import re
from typing import Any

HIGH_CONTEXT_RATIO = 0.75
HIGH_CONTEXT_TOKENS = 160_000
MIN_SUCCESSFUL_RUNS_FOR_TOPIC_CHECK = 3

_TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{2,}")
_FOLLOW_UP_PREFIXES = (
    "also ",
    "and ",
    "but ",
    "can you ",
    "could you ",
    "do that",
    "go ahead",
    "how about ",
    "it ",
    "let's ",
    "now ",
    "please ",
    "that ",
    "then ",
    "this ",
    "what about ",
    "yes",
)
_STOP_WORDS = {
    "about",
    "after",
    "again",
    "also",
    "always",
    "another",
    "anything",
    "before",
    "being",
    "between",
    "change",
    "check",
    "code",
    "could",
    "does",
    "doing",
    "every",
    "from",
    "going",
    "have",
    "help",
    "here",
    "into",
    "just",
    "make",
    "maybe",
    "more",
    "need",
    "only",
    "other",
    "please",
    "really",
    "should",
    "something",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "thing",
    "think",
    "this",
    "those",
    "through",
    "update",
    "using",
    "want",
    "what",
    "when",
    "where",
    "which",
    "while",
    "with",
    "would",
    "you",
    "your",
}


def extract_topic_terms(text: str, limit: int = 12) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for match in _TOKEN_PATTERN.finditer(text.lower()):
        term = match.group(0).strip(".-_")
        if (
            len(term) < 3
            or term in _STOP_WORDS
            or term.isdigit()
            or term in seen
        ):
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= limit:
            break
    return terms


def assess_rollover(
    prompt: str,
    recent_topics: list[list[str]],
    successful_runs: int,
    context_tokens: int | None,
    context_window_tokens: int | None,
) -> str | None:
    if context_tokens is not None:
        if (
            context_window_tokens
            and context_window_tokens > 0
            and context_tokens / context_window_tokens >= HIGH_CONTEXT_RATIO
        ):
            percent = round(100 * context_tokens / context_window_tokens)
            return f"Claude's context is approximately {percent}% full."
        if not context_window_tokens and context_tokens >= HIGH_CONTEXT_TOKENS:
            return (
                "Claude reported a large active context "
                f"({context_tokens:,} input tokens)."
            )

    normalized = prompt.strip().lower()
    if (
        successful_runs < MIN_SUCCESSFUL_RUNS_FOR_TOPIC_CHECK
        or normalized.startswith(_FOLLOW_UP_PREFIXES)
    ):
        return None

    prompt_terms = set(extract_topic_terms(prompt))
    prior_terms = {
        term
        for topic in recent_topics[-8:]
        for term in topic
    }
    if len(prompt_terms) < 5 or len(prior_terms) < 8:
        return None
    if prompt_terms.isdisjoint(prior_terms):
        return "This message appears unrelated to the recent session topics."
    return None


def usage_context_tokens(
    usage: dict[str, Any] | None,
) -> tuple[int | None, int | None]:
    if not usage:
        return None, None

    input_candidates: list[int] = []
    window_candidates: list[int] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return

        normalized = {
            re.sub(r"[^a-z]", "", str(key).lower()): item
            for key, item in value.items()
        }
        input_total = sum(
            _non_negative_int(normalized.get(key))
            for key in (
                "inputtokens",
                "cachereadinputtokens",
                "cachecreationinputtokens",
            )
        )
        if input_total:
            input_candidates.append(input_total)
        for key in ("contextwindow", "contextwindowtokens"):
            window = _non_negative_int(normalized.get(key))
            if window:
                window_candidates.append(window)
        for item in value.values():
            visit(item)

    visit(usage)
    return (
        max(input_candidates) if input_candidates else None,
        max(window_candidates) if window_candidates else None,
    )


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return 0

"""Encoded-query construction for the ServiceNow Table API.

ServiceNow's `IN` operator takes a comma-separated list, which quietly breaks on
any value that itself contains a comma — and real inventory data is full of them
("MacBook Pro (16-inch, 2023)", "Mac16,1", "Acme, Inc."). Those lookups do not
error; they just silently match nothing, leaving `model_id` and `manufacturer`
empty on every affected CI.

So we OR-chain equality terms instead: `name=a^ORname=b^ORname=c`. Commas are
safe inside a value there. A trailing AND clause closes the OR group, which is
how ServiceNow scopes `(a OR b) AND c`.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

# `^` separates conditions in an encoded query, so a value containing one would
# inject an extra condition and silently widen the match.
_QUERY_UNSAFE = ("^",)


def is_query_safe(value: str) -> bool:
    return bool(value) and not any(ch in value for ch in _QUERY_UNSAFE)


def usable_values(values: Iterable[str]) -> list[str]:
    """Trim, drop blanks and unsafe values, and de-duplicate while keeping order."""
    seen: dict[str, None] = {}
    for value in values:
        text = (value or "").strip()
        if is_query_safe(text):
            seen.setdefault(text, None)
    return list(seen)


def build_or_query(field: str, values: list[str], *, and_clause: str | None = None) -> str:
    """Build `field=v1^ORfield=v2[^and_clause]`.

    The AND clause goes last so it applies to the whole OR group rather than
    binding to only the final term.
    """
    if not values:
        raise ValueError("build_or_query requires at least one value")
    query = "^OR".join(f"{field}={value}" for value in values)
    if and_clause:
        query = f"{query}^{and_clause}"
    return query


def chunked(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]

"""Shared fail-closed entropy gate for non-interactive bearer-secret providers.

A service-credential provider (e.g. the drain-control secret, the desktop
API key) provisions a shared bearer secret. That secret must clear an
entropy bar before the provider registers — a weak/short/low-entropy value
is rejected (the provider declines to register and records a skip reason),
never silently accepted. Centralised here so every bearer-secret provider
applies the same bar without copy-pasting the guards.

The bar: >= 43 url-safe-base64 chars (~= 256 bits), >= 16 distinct chars,
and >= 128 Shannon bits over the character distribution. ``token_urlsafe(32)``
produces a 43-char value that clears all three exactly.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Optional

# Default entropy bar: 43 url-safe-base64 chars ~= 256 bits. token_urlsafe(32)
# produces 43 chars, so a correctly-provisioned secret clears this exactly.
DEFAULT_MIN_SECRET_CHARS = 43
# A secret must contain at least this many DISTINCT characters — rejects
# degenerate values like "aaaa..." that are long but trivially low-entropy.
MIN_DISTINCT_CHARS = 16
# Shannon entropy floor (bits) over the secret's characters — a second,
# distribution-aware guard on top of the length + distinct-count checks.
MIN_SHANNON_BITS = 128.0


def shannon_bits(value: str) -> float:
    """Total Shannon entropy (bits) of ``value`` over its character distribution.

    H = len * sum(-p_i * log2(p_i)). A long string drawn from a wide alphabet
    scores high; a long run of one character scores ~0.
    """
    if not value:
        return 0.0
    counts = Counter(value)
    n = len(value)
    per_char = -sum((c / n) * math.log2(c / n) for c in counts.values())
    return per_char * n


def assess_secret_strength(
    secret: str, *, min_chars: int = DEFAULT_MIN_SECRET_CHARS
) -> Optional[str]:
    """Return a rejection reason if ``secret`` is too weak, else ``None``.

    Fail-closed entropy gate. Checks, in order:
      * length >= ``min_chars`` (default 43 url-safe-b64 chars ~= 256 bits),
      * at least ``MIN_DISTINCT_CHARS`` distinct characters,
      * Shannon entropy >= ``MIN_SHANNON_BITS`` bits.

    A ``None`` return means the secret passes. Any string return is a
    human-readable reason the caller logs + records as the skip reason.
    """
    if not secret:
        return "secret is empty"
    if len(secret) < min_chars:
        return (
            f"secret too short: {len(secret)} chars (need >= {min_chars}; "
            "use a >=256-bit value, e.g. `python -c \"import secrets; "
            "print(secrets.token_urlsafe(32))\"`)"
        )
    distinct = len(set(secret))
    if distinct < MIN_DISTINCT_CHARS:
        return (
            f"secret has only {distinct} distinct characters (need >= "
            f"{MIN_DISTINCT_CHARS}); looks structured/low-entropy"
        )
    bits = shannon_bits(secret)
    if bits < MIN_SHANNON_BITS:
        return (
            f"secret entropy too low: {bits:.0f} bits (need >= "
            f"{MIN_SHANNON_BITS:.0f}); looks structured/repeated"
        )
    return None

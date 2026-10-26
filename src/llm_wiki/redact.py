"""Conservative redaction for wiki records."""
from __future__ import annotations

import re

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED_TOKEN]"),
    (re.compile(r"(?im)^Authorization:\s*Basic\s+[A-Za-z0-9._~+/=-]+"), "Authorization: Basic [REDACTED_TOKEN]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_ACCESS_KEY]"),
    (re.compile(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\b(?:sk_live_|sk_test_|rk_live_)[A-Za-z0-9_-]+\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]+\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "[REDACTED_JWT]"),
    (re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@"), r"\1[REDACTED_CREDENTIALS]@"),
    (re.compile(r"""(?i)(["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|aws_secret_access_key|token|password|passwd|secret)["']?\s*[:=]\s*)(["']).*?\2"""), r"\1[REDACTED_SECRET]"),
    (re.compile(r"""(?i)(["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|aws_secret_access_key|token|password|passwd|secret)["']?\s*[:=]\s*)[^\s,;\]\)}]+"""), r"\1[REDACTED_SECRET]"),
    (re.compile(r"(?im)^(Cookie|Set-Cookie)\s*:\s*[^\n]+"), r"\1: [REDACTED_AUTH]"),
    (re.compile(r"(?i)(session|sid)=([^\s,;\]\)}]+)"), r"\1=[REDACTED_TOKEN]"),
)


def redact(text: str) -> tuple[str, int]:
    """Return text with likely secrets replaced and the number of replacements."""
    output = text or ""
    count = 0
    for pattern, replacement in _PATTERNS:
        output, replaced = pattern.subn(replacement, output)
        count += replaced
    return output, count

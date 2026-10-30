"""Conservative redaction for wiki records."""
from __future__ import annotations

import re
from collections.abc import Callable

# A placeholder ends where redact itself would have left it: after a trailing punctuation run that
# meets whitespace or the end, or before closing quotes that end the line (a header value stored with
# json.dumps, whose escapes may add \" before its own closing quote). [REDACTED_SECRET] also replaces
# whole quoted values, so a delimiter right after it ends it too ({"password":[REDACTED_SECRET],"user":..}),
# as the first pass leaves the text after a quoted value. Other placeholders can come from an earlier
# pattern inside a longer value (sk-...,XYZ), and text glued to any placeholder
# (password=[REDACTED_SECRET]hunter2, [REDACTED_TOKEN]",x) does not end it, so both are masked again.
# Whitespace inside one line; a line ends at \n, with an optional \r before it (CRLF checkouts).
# A lone \r inside a line is not a line end, so it separates like any other whitespace.
_HSPACE = r"(?:[^\S\r\n]|\r(?!\n))"
_LINE_END = r"(?=\r?\n|\Z)"
_CLOSING = r"""(?:\\*["'])+"""
_COMPLETE = r"[,;.)\]}]*(?:\s|" + _CLOSING + _LINE_END + "|$)"
# A value that is exactly one earlier placeholder (case-sensitive) is left alone, so redacting
# twice changes nothing and sync can treat any change as a secret.
_NOT_PLACEHOLDER = r"(?!(?-i:\[REDACTED_SECRET\][,;:)\]}]|\[REDACTED_[A-Z_]+\]" + _COMPLETE + "))"
_VALUE = _NOT_PLACEHOLDER + r"(?!" + _CLOSING + _LINE_END + r")(\S+)"
_TRAILING = ",;.)]}"


def _value_masker(prefix_group: int, mask: str) -> Callable[[re.Match[str]], str]:
    """Mask a whitespace-free value token, keeping a trailing punctuation run outside it.

    A value opening with "[" keeps its first closing bracket.
    """

    def replace(match: re.Match[str]) -> str:
        token = match.group(match.lastindex or 0)
        core = token.rstrip(_TRAILING) or token[:1]
        trail = token[len(core):]
        if core.startswith("[") and not core.endswith("]") and "]" in trail:
            cut = trail.index("]") + 1
            core, trail = core + trail[:cut], trail[cut:]
        return match.group(prefix_group) + mask + trail

    return replace


def _key_value(match: re.Match[str]) -> str:
    if match.group(2) is not None:
        suffix = match.group(3) or ""
        trail = suffix[len(suffix.rstrip(_TRAILING)):]
        return match.group(1) + "[REDACTED_SECRET]" + trail
    return _value_unquoted(match)


_value_unquoted = _value_masker(1, "[REDACTED_SECRET]")

# Quoted and unquoted values share one pass so a placeholder written for a quoted value is
# never re-read as the start of an unquoted value. Non-delimited suffixes are masked too, and
# backslash-escaped quotes stay inside the quoted value. Keys and values quoted with escaped quotes
# (JSON inside a JSON string) are recognized; inside those, an escaped backslash or quote of the inner
# string (\\\\, \\\") stays in the value; each escape has exactly one reading so a failed match
# stays linear. A key name may wrap the keyword (SECRET_KEY, x_password); it is read once from the
# start of the name so keyword repeats stay linear, and the separator (:, =, := or =>) is chosen once, never retried
# as a shorter separator. A value, quoted or not, may continue with further quoted
# segments ('it''s', """a b""", "a"' b', abc"d e"). A quote that opens no closed segment masks the
# rest of the line; see _SUFFIX for the short-line delimiter exception. Masking stays within one
# line: separators and values never cross a line break, so fields written on separate lines are
# redacted alike alone and together, and a key whose value starts on the next line is not covered.
# Tags and auth headers are line-local too; only private-key blocks span lines.
_SECRET_WORDS = r"api[_-]?key|token|password|passwd|secret"
_QUOTED = r""""(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'"""
# The first quote that closes no segment ends the scan: when fewer than _SCAN characters remain
# on the line and a delimiter follows the quote, one more unquoted run is allowed and any further
# quote masks the rest of the line; otherwise the rest of the line is masked. A failed scan thus
# either masks the line or covered fewer than _SCAN characters, so a line of many values stays linear.
_SCAN = 256
_SUFFIX = (
    r"""(?=\S)(?:""" + _QUOTED + r"""|[^\s"'])*+"""
    r"""(?:["'](?![^\n]{%d})(?=[\s,;:)\]}]|$)[^\s"']*+(?:["'][^\n]*)?|["'][^\n]*)?""" % _SCAN
)
_KEY_VALUE = re.compile(
    r"""(?i)(\\?["']?(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]*?(?:""" + _SECRET_WORDS + r"""))[A-Za-z0-9_-]++\\?["']?""" + _HSPACE + r"""*(?>:=|=>|[:=])""" + _HSPACE + r"""*)"""
    r"""(?!""" + _CLOSING + _LINE_END + r""")(?:(\\"(?:[^\\\n]|\\\\(?:\\.|[^\\\n])|\\[^"\\\n])*\\"|\\'(?:[^\\\n]|\\\\(?:\\.|[^\\\n])|\\[^'\\\n])*\\'|"""
    + _QUOTED
    + r"""|\\?["'][^\n]*)(?:(?=[\s,;:)\]}]|$)|("""
    + _SUFFIX
    + "))|"
    + _NOT_PLACEHOLDER
    + "("
    + _SUFFIX
    + "))"
)

# A cookie header inside a quoted argument (curl -H "Cookie: ...") is masked up to
# its closing quote when a delimiter follows that quote; anywhere else, including a
# quoted key ("Cookie": "...", or \"Cookie\": inside a JSON string) or text glued after the
# closing quote, the rest of the line is masked. \bCookie also covers Set-Cookie.
# A cookie value the first pass wrote runs to the line end or to its closing quote.
_AUTH_DONE = r"(?!" + _HSPACE + r"*(?-i:\[REDACTED_AUTH\])(?:" + _CLOSING + r"(?=[\s,;:)\]}]|$)|" + _LINE_END + "))"
_COOKIE = re.compile(
    r"(?i)([\"'])(Cookie|Set-Cookie)" + _HSPACE + r"*:" + _AUTH_DONE + r"(?:\\.|(?!\1)[^\n\\])*(?:(\1)(?=[\s,;:)\]}]|$)|[^\n]*)"
    r"|\b(Cookie)(\\?[\"']?)" + _HSPACE + r"*:" + _AUTH_DONE + r"[^\n]*"
)


_PATTERNS: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"(?i)Bearer" + _HSPACE + r"+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED_TOKEN]"),
    (re.compile(r"(?i)\bAuthorization:" + _HSPACE + r"*Basic" + _HSPACE + r"+[A-Za-z0-9._~+/=-]+"), "Authorization: Basic [REDACTED_TOKEN]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_ACCESS_KEY]"),
    (re.compile(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\b(?:sk_live_|sk_test_|rk_live_)[A-Za-z0-9_-]+\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]+\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[REDACTED_TOKEN]"),
    # Each run is entered once at its start; every eyJ in a run shares the same segment ends.
    (re.compile(r"(?<![A-Za-z0-9_-])(?>([A-Za-z0-9_-]*?)\beyJ)[A-Za-z0-9_-]++\.[A-Za-z0-9_-]++\.[A-Za-z0-9_-]+\b"),
     r"\1[REDACTED_JWT]"),
    (re.compile(r"(?i)([a-z][a-z0-9+.-]{0,31}://)[^\s/@:]*:[^\s/@]+@"), r"\1[REDACTED_CREDENTIALS]@"),
    (
        re.compile(r"(?i)(<((?=[\w:-]*?(?:" + _SECRET_WORDS + r"))[\w:-]++)" + _HSPACE + r"*>)[^<\n]*(</\2" + _HSPACE + r"*>)"),
        r"\1[REDACTED_SECRET]\3",
    ),
    (_KEY_VALUE, _key_value),
    # Only one alternative matches; re.sub turns the other's unmatched groups into ''.
    (_COOKIE, r"\1\2\4\5: [REDACTED_AUTH]\3"),
    (re.compile(r"(?i)((?<![\w-])(?=[\w-]*?(?:session|sid(?![\w-])))[\w-]++" + _HSPACE + r"*=" + _HSPACE + r"*)" + _VALUE),
     _value_masker(1, "[REDACTED_TOKEN]")),
)


def redact(text: str) -> tuple[str, int]:
    """Return text with likely secrets replaced and the number of replacements."""
    output = text or ""
    count = 0
    for pattern, replacement in _PATTERNS:
        output, replaced = pattern.subn(replacement, output)
        count += replaced
    return output, count

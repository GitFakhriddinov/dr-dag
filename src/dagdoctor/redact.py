"""Strip secrets and personal data before anything leaves the machine.

Two modes:

* ``redact_log`` is aggressive. Logs are only read, never patched, so it is fine
  to blank out anything that looks like a credential, an e-mail or a phone number.
* ``redact_code`` only blanks out *literal* secrets (quoted values, credentials in
  URIs, well-known token formats). It must leave ordinary code untouched,
  otherwise a patch written against the redacted snippet would not apply to the
  real file.
"""

from __future__ import annotations

import re

_URI_CREDS = (
    re.compile(r"(?P<pre>\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:)[^\s@/]+(?=@)", re.I),
    r"\g<pre>***",
)
_TOKEN_FORMATS = [
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "***"),  # AWS access key id
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "***"),  # LLM provider style keys
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "***"),  # GitHub tokens
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"), "***"),  # Telegram bot tokens
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=\-]{8,}"), r"\1***"),
]
_SECRET_KEYS = r"password|passwd|pwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|token"

# key=value / key: value with any value (logs only)
_LOG_KEY_VALUE = (
    re.compile(rf"""(?i)(?<![A-Za-z0-9])({_SECRET_KEYS})(?![A-Za-z0-9])(\s*[=:]\s*)(['"]?)[^\s'",;&)]+"""),
    r"\1\2\3***",
)
# key=value only when the value is a quoted literal (safe for code)
_CODE_KEY_VALUE = (
    re.compile(
        rf"""(?i)(?<![A-Za-z0-9])({_SECRET_KEYS})(?![A-Za-z0-9])(['"]?\s*[=:]\s*)(['"])(?!\*\*\*)[^'"\n]+\3"""
    ),
    r"\1\2\3***\3",
)
_PII = [
    (re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]*[A-Za-z]"), "<email>"),
    (re.compile(r"\+\d{11,14}\b"), "<phone>"),
]


def _apply(text: str, rules: list[tuple[re.Pattern[str], str]]) -> str:
    for pattern, replacement in rules:
        text = pattern.sub(replacement, text)
    return text


def redact_log(text: str) -> str:
    """Blank out credentials and personal data in a log."""
    return _apply(text, [_URI_CREDS, *_TOKEN_FORMATS, _LOG_KEY_VALUE, *_PII])


def redact_code(text: str) -> str:
    """Blank out literal secrets in source code while keeping it patchable."""
    return _apply(text, [_URI_CREDS, *_TOKEN_FORMATS, _CODE_KEY_VALUE])


def contains_secret(text: str) -> bool:
    """True if ``text`` contains something that looks like a literal secret."""
    return redact_code(text) != text

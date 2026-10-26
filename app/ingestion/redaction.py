"""
redaction.py — remove secrets from documents before they are chunked or indexed.

Runs after loading and before chunking. Each secret is replaced with a
[REDACTED:<kind>] marker, so the text still reads sensibly and the agent
can tell that something was removed.

Secret values are never logged or stored. Only counts per kind are kept.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace

from app.ingestion.loaders import Document

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SecretPattern:
    """
    One kind of secret to find.

    Optional named groups in the regex:
      prefix — text kept before the marker (e.g. "password: ")
      q      — a quote character, kept around the marker
      key    — the setting name, used to skip references like "secretName"
    """
    kind: str
    regex: re.Pattern[str]


# Values that point to a secret instead of containing one:
# ${ENV_VAR}, {{ template }}, <placeholder>, or text already redacted.
_SKIP_VALUE = r"\[REDACTED|\$\{|\{\{|<"

# Setting names that usually hold a secret value.
_SECRET_KEY_NAMES = (
    r"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key"
    r"|private[_-]?key|client[_-]?secret|credentials?)"
)

# Setting names that contain a secret word but hold a reference to one,
# e.g. "secretName: payments-db" in a Kubernetes manifest.
_REFERENCE_KEY_SUFFIXES = ("name", "ref", "path", "file", "type", "ttl", "length")

# "key: value" or "KEY=value" at the start of a line. Covers YAML, the
# indented JSON that loaders.py produces, .env-style lines and
# "export KEY=value" in runbook code blocks. Anchoring to the line start
# keeps it from firing on prose like "Rotate the token: weekly".
_ASSIGNMENT = re.compile(
    r"^(?P<prefix>[ \t]*(?:-[ \t]+|export[ \t]+)?"
    r"(?P<key>[\"']?[\w.-]*" + _SECRET_KEY_NAMES + r"[\w.-]*[\"']?)"
    r"[ \t]*[:=][ \t]*)"
    r"(?:(?P<q>[\"'])(?!" + _SKIP_VALUE + r")[^\"'\n]{4,}(?P=q)"
    r"|(?!" + _SKIP_VALUE + r")[^\s\"',;]{4,})",
    re.IGNORECASE | re.MULTILINE,
)

# Order matters: specific formats run first, so a GitHub token in
# "token: ghp_..." is labeled github_token, not just credential.
SECRET_PATTERNS: tuple[SecretPattern, ...] = (
    SecretPattern(
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    SecretPattern("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    SecretPattern(
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
    ),
    SecretPattern("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    SecretPattern("api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    SecretPattern("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    SecretPattern(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ),
    SecretPattern(
        "bearer_token",
        re.compile(r"(?P<prefix>\bbearer[ \t]+)[A-Za-z0-9._~+/-]{16,}=*", re.IGNORECASE),
    ),
    # postgres://user:password@host -> keeps the user, removes the password.
    SecretPattern(
        "connection_password",
        re.compile(
            r"(?P<prefix>\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)"
            r"(?!\[REDACTED)[^@\s/]+(?=@)",
            re.IGNORECASE,
        ),
    ),
    SecretPattern("credential", _ASSIGNMENT),
)


def _marker(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def _is_reference_key(key: str) -> bool:
    name = key.strip("\"'").lower()
    return name.endswith(_REFERENCE_KEY_SUFFIXES)


def redact_text(text: str) -> tuple[str, dict[str, int]]:
    """
    Replace secrets in text with [REDACTED:<kind>] markers.
    Returns the redacted text and how many secrets of each kind were removed.
    """
    counts: Counter[str] = Counter()

    for pattern in SECRET_PATTERNS:

        def _replace(match: re.Match[str], kind: str = pattern.kind) -> str:
            groups = match.groupdict()
            key = groups.get("key")
            if key and _is_reference_key(key):
                return match.group(0)
            counts[kind] += 1
            quote = groups.get("q") or ""
            return f"{groups.get('prefix') or ''}{quote}{_marker(kind)}{quote}"

        text = pattern.regex.sub(_replace, text)

    return text, dict(counts)


def redact_document(doc: Document) -> Document:
    """
    Return a copy of the document with secrets removed. The input is not changed.

    id and content_hash still identify the source file as it was loaded,
    so document IDs stay stable whether or not anything was redacted.
    """
    text, counts = redact_text(doc.content)

    if counts:
        # Log the kinds and counts only, never the values.
        logger.info(
            "Redacted %d secret(s) from %s: %s",
            sum(counts.values()),
            doc.source_path,
            ", ".join(f"{kind}={n}" for kind, n in sorted(counts.items())),
        )

    metadata = {**doc.metadata, "redacted": bool(counts), "redaction_counts": counts}
    return replace(doc, content=text, metadata=metadata)


def redact_documents(docs: Iterable[Document]) -> list[Document]:
    """Redact a batch of documents."""
    return [redact_document(doc) for doc in docs]
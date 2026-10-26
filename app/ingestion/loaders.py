"""
loaders.py — turn raw ops content into clean, labeled Document records.

This is AgentGuard's first trust boundary. Every Document records:
  - where it came from   (source_path, relative to the data root)
  - who owns it          (team / service — the tenant)
  - how far to trust it  (trust_level, from the document's role)
  - how risky it is      (env, kept separate from trust)

Expected folder layout under the data root:
    data/
      runbooks/      -> runbook     -> trusted
      configs/       -> config      -> trusted
      postmortems/   -> postmortem  -> semi
      tickets/       -> ticket      -> untrusted

Requires: pyyaml  (pip install pyyaml)
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 1 * 1024 * 1024  # 1 MB

TEXT_SUFFIXES = {".md", ".txt"}
STRUCTURED_SUFFIXES = {".yaml", ".yml", ".json"}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | STRUCTURED_SUFFIXES

# A document's role comes from the folder it lives in.
ROLE_BY_FOLDER = {
    "runbooks": "runbook",
    "configs": "config",
    "postmortems": "postmortem",
    "tickets": "ticket",
}

# Trust depends on who could have written the document, not on the environment.
TRUST_BY_ROLE = {
    "runbook": "trusted",
    "config": "trusted",
    "postmortem": "semi",
    "ticket": "untrusted",
}

# Environment is a risk label: prod means more caution, not more trust.
VALID_ENVS = {"prod", "staging", "dev"}


@dataclass
class Document:
    id: str
    source_path: str
    team: str
    service: str
    env: str
    doc_type: str
    trust_level: str
    content: str
    content_hash: str
    loaded_at: str
    metadata: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _read_text_safely(path: Path, max_bytes: int) -> str:
    """Read a file as text, refusing anything larger than max_bytes."""
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"File is {size} bytes, limit is {max_bytes}")

    # Read at most max_bytes + 1 so a file that grows after the size
    # check still can't make us read past the limit.
    with path.open("rb") as f:
        raw = f.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"File exceeds limit of {max_bytes} bytes")

    # Invalid bytes become '�' instead of stopping the whole load.
    return raw.decode("utf-8", errors="replace")


def _normalize_text(text: str) -> str:
    """Use one line-ending style so identical content always hashes the same."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _infer_doc_type(path: Path) -> str:
    """Map the parent folder name to a document role."""
    folder = path.parent.name.lower()
    if folder not in ROLE_BY_FOLDER:
        raise ValueError(f"Unknown document folder: {folder!r}")
    return ROLE_BY_FOLDER[folder]


def _assign_trust_level(doc_type: str) -> str:
    """Return the trust level for a document role."""
    if doc_type not in TRUST_BY_ROLE:
        raise ValueError(f"No trust level defined for doc type: {doc_type!r}")
    return TRUST_BY_ROLE[doc_type]


def _normalize_env(env: str) -> str:
    env = env.strip().lower()
    if env not in VALID_ENVS:
        raise ValueError(f"Unknown environment: {env!r}")
    return env


def _normalize_name(value: str, field_name: str) -> str:
    value = value.strip().lower()
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    return value


def _render_structured(text: str, suffix: str) -> str:
    """
    Parse YAML/JSON to validate it, then re-render it as readable text.
    yaml.safe_load only builds plain data; yaml.load can run code.
    """
    try:
        if suffix in {".yaml", ".yml"}:
            data = yaml.safe_load(text)
            return yaml.safe_dump(data, sort_keys=False)
        data = json.loads(text)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not parse {suffix} file: {exc}") from exc


def _content_hash(text: str) -> str:
    """SHA-256 of the normalized text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _document_id(team: str, content_hash: str) -> str:
    """
    Stable document ID. The team is included so the same file loaded by
    two tenants gets two different IDs.
    """
    return hashlib.sha256(f"{team}:{content_hash}".encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def load_file(
    path: Path,
    root: Path,
    team: str,
    service: str,
    env: str,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Document:
    """
    Build the full Document record for one file.
    Raises ValueError or OSError if the file should not be loaded.
    """
    root = root.resolve()
    resolved = path.resolve()

    # A symlink inside the data root must not point outside it.
    if not resolved.is_relative_to(root):
        raise ValueError(f"Path escapes data root: {path}")

    suffix = resolved.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(f"Unsupported file type: {suffix!r}")

    doc_type = _infer_doc_type(resolved)
    trust_level = _assign_trust_level(doc_type)

    text = _normalize_text(_read_text_safely(resolved, max_bytes))
    if suffix in STRUCTURED_SUFFIXES:
        text = _render_structured(text, suffix)

    team = _normalize_name(team, "team")
    content_hash = _content_hash(text)

    return Document(
        id=_document_id(team, content_hash),
        # Relative path with forward slashes, so no machine details are stored.
        source_path=resolved.relative_to(root).as_posix(),
        team=team,
        service=_normalize_name(service, "service"),
        env=_normalize_env(env),
        doc_type=doc_type,
        trust_level=trust_level,
        content=text,
        content_hash=content_hash,
        loaded_at=datetime.now(timezone.utc).isoformat(),
        metadata={"format": suffix.lstrip(".")},
    )


def load_directory(
    root: Path,
    team: str,
    service: str,
    env: str,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> list[Document]:
    """
    Load every valid file under root, skip and log the rest,
    and drop duplicates by content hash.
    """
    root = root.resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")

    documents: list[Document] = []
    seen_hashes: set[str] = set()
    skipped = 0

    # sorted() keeps the load order the same on every run.
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)

        # Skip hidden files and folders (.git, .DS_Store, ...).
        if any(part.startswith(".") for part in rel.parts):
            continue
        if not path.is_file():
            continue

        try:
            doc = load_file(path, root, team, service, env, max_bytes)
        except (ValueError, OSError) as exc:
            logger.warning("Skipping %s: %s", rel.as_posix(), exc)
            skipped += 1
            continue

        if doc.content_hash in seen_hashes:
            logger.info("Skipping duplicate: %s", rel.as_posix())
            skipped += 1
            continue

        seen_hashes.add(doc.content_hash)
        documents.append(doc)

    logger.info("Loaded %d documents, skipped %d", len(documents), skipped)
    return documents
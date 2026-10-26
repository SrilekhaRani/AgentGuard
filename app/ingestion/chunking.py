"""
chunking.py — split redacted documents into chunks for embedding and search.

Pipeline position:  load -> redact -> chunk -> embed -> index

How a document is split:
  - Markdown / text: by headings first, then paragraphs, then lines,
    sentences and words if a piece is still too long.
    Code blocks (``` or ~~~) are never split.
  - YAML / JSON: one section per top-level key.

Small sections are merged with the next one, and a little text is
repeated between neighbouring chunks (overlap) so an instruction cut
at a boundary still appears whole somewhere.

Every chunk inherits its document's team, service, env and trust_level.
Losing trust_level here would let an untrusted ticket's text look trusted
to the guardrail layer, so it is copied onto every chunk.

Sizes are measured in characters (about 4 characters per token).
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

from app.ingestion.models import Chunk, Document

logger = logging.getLogger(__name__)

STRUCTURED_FORMATS = {"yaml", "yml", "json"}

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_YAML_TOP_LEVEL_KEY = re.compile(r"^([^\s#-][^:]*):")
_JSON_TOP_LEVEL_ITEM = re.compile(r"^  [^\s}\]]")
_JSON_KEY = re.compile(r'^\s*"((?:[^"\\]|\\.)*)"\s*:')


@dataclass(frozen=True)
class ChunkingConfig:
    max_chars: int = 1500      # about 375 tokens
    overlap_chars: int = 200   # repeated from the end of the previous chunk
    min_chars: int = 200       # sections smaller than this are merged forward
    require_redaction: bool = True

    def __post_init__(self) -> None:
        if self.max_chars <= 0:
            raise ValueError("max_chars must be positive")
        if not 0 <= self.overlap_chars < self.max_chars:
            raise ValueError("overlap_chars must be >= 0 and smaller than max_chars")
        if not 0 <= self.min_chars <= self.max_chars:
            raise ValueError("min_chars must be between 0 and max_chars")


DEFAULT_CONFIG = ChunkingConfig()


@dataclass
class _Block:
    text: str
    is_code: bool = False


@dataclass
class _Section:
    heading: str
    blocks: list[_Block]

    def size(self) -> int:
        return sum(len(b.text) for b in self.blocks)


# --------------------------------------------------------------------------
# Parsing documents into sections
# --------------------------------------------------------------------------

def _parse_markdown(text: str) -> list[_Section]:
    """Split markdown into sections by heading, and each section into blocks."""
    sections: list[_Section] = []
    heading_stack: list[tuple[int, str]] = []
    current = _Section(heading="", blocks=[])
    paragraph: list[str] = []
    code: list[str] = []
    fence = ""

    def flush_paragraph() -> None:
        if paragraph:
            block = "\n".join(paragraph).strip()
            if block:
                current.blocks.append(_Block(block))
            paragraph.clear()

    def flush_code() -> None:
        if code:
            current.blocks.append(_Block("\n".join(code), is_code=True))
            code.clear()

    for line in text.split("\n"):
        if fence:
            code.append(line)
            if line.strip().startswith(fence):
                fence = ""
                flush_code()
            continue

        fence_match = _FENCE.match(line)
        if fence_match:
            flush_paragraph()
            fence = fence_match.group(1)
            code.append(line)
            continue

        heading_match = _HEADING.match(line)
        if heading_match:
            flush_paragraph()
            if current.blocks:
                sections.append(current)
            level = len(heading_match.group(1))
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, heading_match.group(2).strip()))
            current = _Section(
                heading=" > ".join(title for _, title in heading_stack),
                blocks=[_Block(line.strip())],
            )
            continue

        if not line.strip():
            flush_paragraph()
        else:
            paragraph.append(line)

    flush_paragraph()
    flush_code()  # an unclosed fence still stays in one piece
    if current.blocks:
        sections.append(current)
    return sections


def _parse_structured(text: str, fmt: str) -> list[_Section]:
    """Split rendered YAML/JSON into one section per top-level key."""
    lines = text.split("\n")
    is_json = fmt == "json"
    if is_json and lines and lines[0].strip() in {"{", "["}:
        lines = lines[1:]
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and lines[-1].strip() in {"}", "]"}:
            lines = lines[:-1]

    sections: list[_Section] = []
    current: list[str] = []
    heading = ""

    def flush() -> None:
        block = "\n".join(current).strip()
        if block:
            sections.append(_Section(heading=heading, blocks=[_Block(block)]))
        current.clear()

    for line in lines:
        if is_json:
            starts_item = bool(_JSON_TOP_LEVEL_ITEM.match(line))
            key_match = _JSON_KEY.match(line) if starts_item else None
        else:
            key_match = _YAML_TOP_LEVEL_KEY.match(line)
            starts_item = bool(key_match)

        if starts_item:
            flush()
            heading = key_match.group(1).strip().strip("\"'") if key_match else ""
        current.append(line)

    flush()
    return sections


def _merge_small_sections(sections: list[_Section], min_chars: int) -> list[_Section]:
    """Merge a too-small section into the one after it (e.g. a lone heading)."""
    merged: list[_Section] = []
    for section in sections:
        if merged and merged[-1].size() < min_chars:
            merged[-1].blocks.extend(section.blocks)
        else:
            merged.append(_Section(section.heading, list(section.blocks)))
    return merged


# --------------------------------------------------------------------------
# Packing sections into size-limited chunks
# --------------------------------------------------------------------------

def _greedy_join(parts: list[str], max_chars: int, joiner: str) -> list[str]:
    out: list[str] = []
    current = ""
    for part in parts:
        if not current:
            current = part
        elif len(current) + len(joiner) + len(part) <= max_chars:
            current = current + joiner + part
        else:
            out.append(current)
            current = part
    if current:
        out.append(current)
    return out


def _split_long_text(text: str, max_chars: int) -> list[str]:
    """Split text over max_chars by lines, then sentences, then words, then characters."""
    if len(text) <= max_chars:
        return [text]
    for splitter, joiner in (
        (lambda t: t.split("\n"), "\n"),
        (_SENTENCE_BREAK.split, " "),
        (str.split, " "),
    ):
        parts = [p for p in splitter(text) if p.strip()]
        if len(parts) > 1:
            pieces: list[str] = []
            for part in parts:
                pieces.extend(_split_long_text(part, max_chars))
            return _greedy_join(pieces, max_chars, joiner)
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def _tail(text: str, n: int) -> str:
    """The last ~n characters of text, starting at a word boundary."""
    if n <= 0:
        return ""
    if len(text) <= n:
        return text
    tail = text[-n:]
    space = tail.find(" ")
    return tail[space + 1:] if space != -1 else tail


def _pack(blocks: list[_Block], max_chars: int, overlap_chars: int) -> list[str]:
    """Pack blocks into chunks of at most max_chars, with overlap between them."""
    pieces: list[_Block] = []
    for block in blocks:
        if block.is_code or len(block.text) <= max_chars:
            pieces.append(block)
        else:
            pieces.extend(_Block(p) for p in _split_long_text(block.text, max_chars))

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    last_was_code = False

    for piece in pieces:
        added = len(piece.text) + (2 if current else 0)
        if current and size + added > max_chars:
            chunks.append("\n\n".join(current))
            # Never start a chunk with half a code block.
            overlap = "" if last_was_code else _tail(current[-1], overlap_chars)
            if overlap and len(overlap) + 2 + len(piece.text) <= max_chars:
                current, size = [overlap], len(overlap)
            else:
                current, size = [], 0
            added = len(piece.text) + (2 if current else 0)
        current.append(piece.text)
        size += added
        last_was_code = piece.is_code

    if current:
        chunks.append("\n\n".join(current))
    return chunks


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def _chunk_id(doc_id: str, index: int) -> str:
    return hashlib.sha256(f"{doc_id}:{index}".encode("utf-8")).hexdigest()[:16]


def chunk_document(doc: Document, config: ChunkingConfig = DEFAULT_CONFIG) -> list[Chunk]:
    """Split one document into chunks that carry its metadata."""
    if config.require_redaction and "redacted" not in doc.metadata:
        raise ValueError(
            f"{doc.source_path} has not been through redaction; "
            "run redact_document() before chunking"
        )

    if not doc.content.strip():
        return []

    fmt = str(doc.metadata.get("format", "")).lower()
    if fmt in STRUCTURED_FORMATS:
        sections = _parse_structured(doc.content, fmt)
        overlap = 0  # half a YAML key repeated in the next chunk only confuses things
    else:
        sections = _parse_markdown(doc.content)
        overlap = config.overlap_chars

    sections = _merge_small_sections(sections, config.min_chars)

    chunks: list[Chunk] = []
    for section in sections:
        for text in _pack(section.blocks, config.max_chars, overlap):
            text = text.strip()
            if not text:
                continue
            index = len(chunks)
            label = f"{doc.source_path} > {section.heading}" if section.heading else doc.source_path
            chunks.append(
                Chunk(
                    chunk_id=_chunk_id(doc.id, index),
                    doc_id=doc.id,
                    chunk_index=index,
                    text=text,
                    embedding_text=f"[{label}]\n{text}",
                    heading=section.heading,
                    source_path=doc.source_path,
                    team=doc.team,
                    service=doc.service,
                    env=doc.env,
                    doc_type=doc.doc_type,
                    trust_level=doc.trust_level,
                    redacted=bool(doc.metadata.get("redacted", False)),
                    metadata={
                        "format": fmt,
                        "has_code": "```" in text or "~~~" in text,
                        "oversized": len(text) > config.max_chars,
                    },
                )
            )

    logger.debug("Split %s into %d chunks", doc.source_path, len(chunks))
    return chunks


def chunk_documents(
    docs: Iterable[Document], config: ChunkingConfig = DEFAULT_CONFIG
) -> list[Chunk]:
    """Chunk a batch of documents."""
    chunks: list[Chunk] = []
    for doc in docs:
        chunks.extend(chunk_document(doc, config))
    return chunks
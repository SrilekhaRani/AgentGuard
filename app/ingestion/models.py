"""
models.py — the data shapes shared by the ingestion pipeline.

Document: one loaded (and later redacted) source file.
Chunk:    one piece of a document, ready for embedding and search.

Both are frozen Pydantic models, so values are checked when an object is
created and cannot be changed afterwards. To change one, make a copy with
model.model_copy(update={...}).
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DocType(StrEnum):
    RUNBOOK = "runbook"
    CONFIG = "config"
    POSTMORTEM = "postmortem"
    TICKET = "ticket"
    LOG = "log"


class Environment(StrEnum):
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class TrustLevel(StrEnum):
    """How far a document's text can be believed. Depends on who could have written it."""
    TRUSTED = "trusted"
    SEMI = "semi"
    UNTRUSTED = "untrusted"


# --------------------------------------------------------------------------
# Shared validation
# --------------------------------------------------------------------------

def _clean_name(value: str, field_name: str) -> str:
    value = value.strip().lower()
    if not value:
        raise ValueError(f"{field_name} must not be blank")
    return value


def _check_relative_path(value: str) -> str:
    """Stored paths must be relative and stay inside the data root."""
    if not value.strip():
        raise ValueError("source_path must not be blank")
    if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
        raise ValueError(f"source_path must be relative: {value!r}")
    if ".." in PurePosixPath(value.replace("\\", "/")).parts:
        raise ValueError(f"source_path must not contain '..': {value!r}")
    return value


class _TenantFields(BaseModel):
    """Fields that say who owns the data. Shared by Document and Chunk."""
    model_config = ConfigDict(frozen=True)

    source_path: str
    team: str
    service: str
    env: Environment
    doc_type: DocType
    trust_level: TrustLevel

    @field_validator("team", "service")
    @classmethod
    def _names_clean(cls, v: str, info: Any) -> str:
        return _clean_name(v, info.field_name)

    @field_validator("env", mode="before")
    @classmethod
    def _env_lowercase(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("source_path")
    @classmethod
    def _path_relative(cls, v: str) -> str:
        return _check_relative_path(v)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class Document(_TenantFields):
    id: str
    content: str
    content_hash: str
    loaded_at: datetime
    owner: str | None = None
    last_reviewed: date | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("content must not be blank")
        return v


class Chunk(_TenantFields):
    chunk_id: str
    doc_id: str
    chunk_index: int = Field(ge=0)
    text: str
    embedding_text: str       # text with a "[source > heading]" prefix, used for embedding
    heading: str = ""
    redacted: bool
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must not be blank")
        return v
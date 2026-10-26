"""
Tests for models.py.

Run from the project root with:
    python -m pytest tests/test_models.py -v
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from app.ingestion.models import Chunk, DocType, Document, Environment, TrustLevel


def doc_fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = dict(
        id="doc123",
        source_path="runbooks/restart.md",
        team="payments",
        service="api",
        env="prod",
        doc_type="runbook",
        trust_level="trusted",
        content="# Restart\n",
        content_hash="hash123",
        loaded_at="2026-10-26T00:00:00+00:00",
    )
    fields.update(overrides)
    return fields


def chunk_fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = dict(
        chunk_id="c1",
        doc_id="doc123",
        chunk_index=0,
        text="Restart the pods.",
        embedding_text="[runbooks/restart.md]\nRestart the pods.",
        source_path="runbooks/restart.md",
        team="payments",
        service="api",
        env="prod",
        doc_type="runbook",
        trust_level="trusted",
        redacted=False,
    )
    fields.update(overrides)
    return fields


# --------------------------------------------------------------------------
# Document
# --------------------------------------------------------------------------

def test_valid_document_converts_types():
    doc = Document.model_validate(doc_fields())
    assert doc.env is Environment.PROD
    assert doc.doc_type is DocType.RUNBOOK
    assert doc.trust_level is TrustLevel.TRUSTED
    assert isinstance(doc.loaded_at, datetime)
    assert doc.owner is None
    assert doc.last_reviewed is None
    assert doc.metadata == {}


def test_optional_owner_and_review_date():
    doc = Document.model_validate(doc_fields(owner="sre-team", last_reviewed="2026-09-01"))
    assert doc.owner == "sre-team"
    assert doc.last_reviewed == date(2026, 9, 1)


def test_team_and_service_are_cleaned():
    doc = Document.model_validate(doc_fields(team="  Payments ", service=" API"))
    assert (doc.team, doc.service) == ("payments", "api")


def test_env_is_case_insensitive():
    assert Document.model_validate(doc_fields(env=" PROD ")).env is Environment.PROD


@pytest.mark.parametrize("field", ["team", "service", "content"])
def test_blank_values_are_rejected(field):
    with pytest.raises(ValidationError):
        Document.model_validate(doc_fields(**{field: "   "}))


@pytest.mark.parametrize(
    "field, value",
    [
        ("env", "qa"),
        ("doc_type", "spreadsheet"),
        ("trust_level", "high"),
    ],
)
def test_unknown_enum_values_are_rejected(field, value):
    with pytest.raises(ValidationError):
        Document.model_validate(doc_fields(**{field: value}))


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "C:\\Users\\srile\\secret.md", "../outside.md", "runbooks/../../x.md", ""],
)
def test_unsafe_source_paths_are_rejected(path):
    with pytest.raises(ValidationError):
        Document.model_validate(doc_fields(source_path=path))


def test_document_is_frozen():
    doc = Document.model_validate(doc_fields())
    with pytest.raises(ValidationError):
        doc.trust_level = TrustLevel.TRUSTED  # type: ignore[misc]


def test_model_copy_makes_a_changed_copy():
    doc = Document.model_validate(doc_fields())
    copy = doc.model_copy(update={"content": "changed\n"})
    assert copy.content == "changed\n"
    assert doc.content == "# Restart\n"


def test_enums_compare_equal_to_plain_strings():
    doc = Document.model_validate(doc_fields())
    assert doc.trust_level == "trusted"
    assert f"{doc.env}" == "prod"


# --------------------------------------------------------------------------
# Chunk
# --------------------------------------------------------------------------

def test_valid_chunk():
    chunk = Chunk.model_validate(chunk_fields(trust_level="untrusted", doc_type="ticket"))
    assert chunk.trust_level is TrustLevel.UNTRUSTED
    assert chunk.heading == ""


def test_chunk_rejects_negative_index():
    with pytest.raises(ValidationError):
        Chunk.model_validate(chunk_fields(chunk_index=-1))


def test_chunk_rejects_blank_text():
    with pytest.raises(ValidationError):
        Chunk.model_validate(chunk_fields(text="  "))


def test_chunk_is_frozen():
    chunk = Chunk.model_validate(chunk_fields(trust_level="untrusted"))
    with pytest.raises(ValidationError):
        chunk.trust_level = TrustLevel.TRUSTED  # type: ignore[misc]
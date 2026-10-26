"""
Tests for redaction.py.

Fake secrets are built by joining strings (e.g. "ghp_" + "a" * 36) so that
GitHub's secret scanning doesn't block the push for looking like real keys.

Run from the project root with:
    python -m pytest tests/test_redaction.py -v
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from app.ingestion.loaders import load_directory
from app.ingestion.models import DocType, Document, Environment, TrustLevel
from app.ingestion.redaction import redact_document, redact_text

GITHUB_TOKEN = "ghp_" + "a1B2" * 9
SLACK_TOKEN = "xoxb-" + "1234567890-" + "abcdefghij"
OPENAI_STYLE_KEY = "sk-" + "Zx9" * 10
GOOGLE_KEY = "AIza" + "B" * 35
JWT = "eyJ" + "h" * 20 + ".eyJ" + "p" * 20 + "." + "s" * 20
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"  # AWS's own documentation example


def make_doc(content: str) -> Document:
    return Document(
        id="abc123",
        source_path="configs/app.yaml",
        team="payments",
        service="api",
        env=Environment.PROD,
        doc_type=DocType.CONFIG,
        trust_level=TrustLevel.TRUSTED,
        content=content,
        content_hash="hash123",
        loaded_at=datetime(2026, 10, 26, tzinfo=timezone.utc),
        metadata={"format": "yaml"},
    )


# --------------------------------------------------------------------------
# Known token formats
# --------------------------------------------------------------------------

def test_aws_access_key():
    text, counts = redact_text(f"aws_access_key_id = {AWS_KEY}\n")
    assert AWS_KEY not in text
    assert "[REDACTED:aws_access_key]" in text
    assert counts == {"aws_access_key": 1}


def test_github_token_gets_specific_label():
    text, counts = redact_text(f"token: {GITHUB_TOKEN}\n")
    assert GITHUB_TOKEN not in text
    assert text == "token: [REDACTED:github_token]\n"
    assert counts == {"github_token": 1}


def test_slack_token():
    text, counts = redact_text(f"Webhook uses {SLACK_TOKEN} for alerts")
    assert SLACK_TOKEN not in text
    assert counts == {"slack_token": 1}


def test_llm_api_key():
    text, counts = redact_text(f"OPENAI_API_KEY={OPENAI_STYLE_KEY}")
    assert OPENAI_STYLE_KEY not in text
    assert counts == {"api_key": 1}


def test_google_api_key():
    text, counts = redact_text(f"maps key {GOOGLE_KEY}")
    assert GOOGLE_KEY not in text
    assert counts == {"google_api_key": 1}


def test_private_key_block_is_removed_whole():
    block = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAfakefakefake\n"
        "moreFakeKeyMaterialHere\n"
        "-----END RSA PRIVATE KEY-----"
    )
    text, counts = redact_text(f"Deploy key:\n{block}\nDone.")
    assert "MIIEow" not in text
    assert text == "Deploy key:\n[REDACTED:private_key]\nDone."
    assert counts == {"private_key": 1}


def test_jwt_in_authorization_header():
    text, counts = redact_text(f"Authorization: Bearer {JWT}")
    assert JWT not in text
    assert text == "Authorization: Bearer [REDACTED:jwt]"
    assert counts == {"jwt": 1}


def test_opaque_bearer_token():
    token = "x7" * 15
    text, counts = redact_text(f"curl -H 'Authorization: Bearer {token}'")
    assert token not in text
    assert counts == {"bearer_token": 1}


def test_connection_string_keeps_user_and_host():
    url = "postgres://admin:s3cretPass@db.internal:5432/payments"
    text, counts = redact_text(f"DATABASE_URL: {url}")
    assert "s3cretPass" not in text
    assert "postgres://admin:[REDACTED:connection_password]@db.internal:5432/payments" in text
    assert counts == {"connection_password": 1}


# --------------------------------------------------------------------------
# key: value settings
# --------------------------------------------------------------------------

def test_yaml_password():
    text, counts = redact_text("db:\n  host: db.internal\n  password: hunter22\n")
    assert "hunter22" not in text
    assert "  password: [REDACTED:credential]" in text
    assert "host: db.internal" in text
    assert counts == {"credential": 1}


def test_json_api_key_keeps_quotes():
    text, counts = redact_text('{\n  "api_key": "abcd1234efgh"\n}')
    assert "abcd1234efgh" not in text
    assert '"api_key": "[REDACTED:credential]"' in text
    assert counts == {"credential": 1}


def test_quoted_password_with_spaces():
    text, _ = redact_text('password: "correct horse battery"\n')
    assert "horse" not in text


def test_env_and_export_lines():
    text, counts = redact_text("export DB_PASSWORD=hunter22\nCLIENT_SECRET=abc123xyz\n")
    assert "hunter22" not in text
    assert "abc123xyz" not in text
    assert counts == {"credential": 2}


def test_yaml_list_item():
    text, _ = redact_text("users:\n  - password: hunter22\n")
    assert "hunter22" not in text


# --------------------------------------------------------------------------
# Things that must NOT be redacted
# --------------------------------------------------------------------------

def test_env_var_and_template_references_are_kept():
    source = (
        "password: ${DB_PASSWORD}\n"
        "token: {{ .Values.token }}\n"
        "api_key: <your-key-here>\n"
    )
    text, counts = redact_text(source)
    assert text == source
    assert counts == {}


def test_kubernetes_secret_reference_is_kept():
    source = "secretName: payments-db-creds\ntokenType: rotating\n"
    text, counts = redact_text(source)
    assert text == source
    assert counts == {}


def test_prose_is_not_redacted():
    source = "Rotate the token: weekly, and check the password: policy page.\n"
    text, counts = redact_text(source)
    assert text == source
    assert counts == {}


def test_clean_text_is_unchanged():
    source = "# Restart payments\n\n1. kubectl rollout restart deploy/payments\n"
    text, counts = redact_text(source)
    assert text == source
    assert counts == {}


def test_running_twice_changes_nothing():
    source = (
        f"password: hunter22\ntoken: {GITHUB_TOKEN}\n"
        "url: postgres://admin:s3cretPass@db:5432/app\n"
    )
    once, _ = redact_text(source)
    twice, counts = redact_text(once)
    assert twice == once
    assert counts == {}


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------

def test_redact_document_returns_copy_with_metadata():
    original = make_doc("password: hunter22\n")
    redacted = redact_document(original)

    assert "hunter22" in original.content  # input untouched
    assert "hunter22" not in redacted.content
    assert redacted.metadata["redacted"] is True
    assert redacted.metadata["redaction_counts"] == {"credential": 1}
    assert redacted.metadata["format"] == "yaml"  # existing metadata kept
    assert redacted.id == original.id


def test_clean_document_is_marked_not_redacted():
    redacted = redact_document(make_doc("replicas: 3\n"))
    assert redacted.metadata["redacted"] is False
    assert redacted.metadata["redaction_counts"] == {}


def test_secret_values_never_reach_logs(caplog):
    with caplog.at_level(logging.DEBUG):
        redact_document(make_doc(f"password: hunter22\ntoken: {GITHUB_TOKEN}\n"))
    assert "hunter22" not in caplog.text
    assert GITHUB_TOKEN not in caplog.text
    assert "credential=1" in caplog.text


def test_end_to_end_with_loader(tmp_path: Path):
    root = tmp_path / "data"
    (root / "configs").mkdir(parents=True)
    (root / "configs" / "db.yaml").write_text(
        "database:\n  user: app\n  password: hunter22\n", encoding="utf-8"
    )
    (root / "tickets").mkdir()
    (root / "tickets" / "t1.json").write_text(
        '{"title": "Login broken", "api_key": "abcd1234efgh"}', encoding="utf-8"
    )

    docs = [redact_document(d) for d in load_directory(root, "payments", "api", "dev")]

    assert len(docs) == 2
    for d in docs:
        assert "hunter22" not in d.content
        assert "abcd1234efgh" not in d.content
        assert d.metadata["redacted"] is True
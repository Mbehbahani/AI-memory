"""Unit tests for :mod:`aimemory.sources.secrets` (P6-T01).

Includes the "no content leakage anywhere" acceptance check: captures actual log records (via
``structlog``/stdlib logging capture) produced from scanning the adversarial fake-secret fixtures and
asserts the raw secret substrings never appear in them.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml
from aimemory.common.logging import configure_logging, get_logger
from aimemory.sources.secrets import SecretDetectorConfig, scan, scan_content, scan_filename

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICIES_YAML = REPO_ROOT / "config" / "policies.yaml"
ADVERSARIAL_DIR = REPO_ROOT / "tests" / "fixtures" / "adversarial"


@pytest.fixture(scope="module")
def config() -> SecretDetectorConfig:
    data = yaml.safe_load(POLICIES_YAML.read_text(encoding="utf-8"))
    return SecretDetectorConfig.from_mapping(data["secret_detector"])


class TestFilenamePatterns:
    @pytest.mark.parametrize(
        "path",
        [".env", ".env.local", "secrets.yaml", "credentials.json", "id_rsa", "id_ed25519.pub", "x.pem", "x.key"],
    )
    def test_matches(self, config: SecretDetectorConfig, path: str) -> None:
        result = scan_filename(path, config)
        assert result.suspected is True, path

    @pytest.mark.parametrize("path", ["notes.md", "README.md", "app.py", "settings.toml"])
    def test_does_not_match_ordinary_files(self, config: SecretDetectorConfig, path: str) -> None:
        result = scan_filename(path, config)
        assert result.suspected is False, path


class TestContentPatterns:
    @pytest.mark.parametrize(
        "text,rule",
        [
            ("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE", "aws_access_key"),
            ("-----BEGIN RSA PRIVATE KEY-----\nZm9v\n-----END RSA PRIVATE KEY-----", "private_key"),
            (
                "eyJhbGciOiJIUzI1NiJ9.eyJmaXh0dXJlIjp0cnVlfQ.c2lnbmF0dXJlLXBhcnQtaGVyZQ",
                "jwt",
            ),
            ("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz0123456789", "openai_style_key"),
            ("token = ghp_" + "a" * 36, "github_token"),
            ("DATABASE_URL=postgres://user:hunter2@localhost/db", "conn_string_pw"),
            ("url?sig=" + "A" * 40, "azure_sas"),
        ],
    )
    def test_matches_known_pattern(self, config: SecretDetectorConfig, text: str, rule: str) -> None:
        result = scan_content(text, config)
        assert result.suspected is True
        assert any(rule in r for r in result.matched_rules)

    def test_ordinary_prose_does_not_match(self, config: SecretDetectorConfig) -> None:
        text = (
            "This design decision selects PostgreSQL as the system of record with Neo4j as a "
            "rebuildable projection, per ADR-0001. No credentials appear anywhere in this text."
        )
        result = scan_content(text, config)
        assert result.suspected is False
        assert result.match_count == 0

    def test_multiple_matches_are_counted(self, config: SecretDetectorConfig) -> None:
        text = "AKIAIOSFODNN7EXAMPLE and again AKIAABCDEFGHIJKLMNOP"
        result = scan_content(text, config)
        assert result.match_count == 2


class TestScanCombination:
    def test_filename_and_content_merge(self, config: SecretDetectorConfig) -> None:
        result = scan(".env", b"AKIAIOSFODNN7EXAMPLE", config)
        assert result.suspected is True
        assert any(r.startswith("filename:") for r in result.matched_rules)
        assert any(r.startswith("content:") for r in result.matched_rules)

    def test_none_content_only_checks_filename(self, config: SecretDetectorConfig) -> None:
        result = scan(".env", None, config)
        assert result.suspected is True
        assert all(r.startswith("filename:") for r in result.matched_rules)

    def test_result_never_carries_raw_content(self, config: SecretDetectorConfig) -> None:
        secret = "AKIAIOSFODNN7EXAMPLE"
        result = scan(".env", secret.encode(), config)
        dump = repr(result)
        assert secret not in dump


class TestNoContentLeakageInLogs:
    """Acceptance: 'no content leakage anywhere, asserted by capturing log records'."""

    def test_scanning_fake_secret_fixtures_never_logs_their_content(
        self, config: SecretDetectorConfig, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
    ) -> None:
        configure_logging(level="INFO", fmt="json")
        logger = get_logger(__name__)
        secret_substrings = [
            "AKIAIOSFODNN7EXAMPLE",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "not-a-real-password",
            "ThisIsNotARealPrivateKeyItIsAFixtureUsedOnlyToExerciseTheSecretDetectorRegex",
        ]
        fixture_dir = ADVERSARIAL_DIR / "fake-secrets"
        for path in sorted(fixture_dir.iterdir()):
            if not path.is_file():
                continue
            content = path.read_bytes()
            result = scan(path.name, content, config)
            # The only thing a caller is able to log is the sanitized result - there is no API on
            # SecretScanResult that returns raw content, so this is the realistic worst case for a
            # careless `logger.info("secret scan", **vars(result))`-style call.
            logger.info(
                "secret_scan",
                path=path.name,
                suspected=result.suspected,
                matched_rules=result.matched_rules,
                match_count=result.match_count,
                reason=result.reason,
            )

        captured = capsys.readouterr()
        combined = captured.out + captured.err + "\n".join(r.message for r in caplog.records)
        for secret in secret_substrings:
            assert secret not in combined, f"leaked secret content into logs: {secret!r}"
        # Sanity: the log records did fire and did carry the rule names (i.e. this is not a
        # false-negative test where nothing was logged at all).
        assert "aws_access_key" in combined or "filename:.env" in combined

    def test_secret_detected_error_never_carries_content(self) -> None:
        from aimemory.common.errors import SecretDetectedError

        err = SecretDetectedError()
        assert "AKIA" not in str(err)
        assert err.sanitized()["message"] == err.public_message

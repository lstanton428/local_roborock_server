from datetime import datetime, timedelta, timezone
from pathlib import Path
import logging
import subprocess

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest
from roborock_local_server.certs import CertificateManager
from roborock_local_server.config import load_config, resolve_paths


def _write_certificate(
    cert_path: Path,
    *,
    common_name: str,
    san_names: list[str],
    valid_days: int = 30,
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=valid_days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name) for name in san_names]), critical=False)
        .sign(private_key=private_key, algorithm=hashes.SHA256())
    )
    cert_path.write_bytes(certificate.public_bytes(encoding=serialization.Encoding.PEM))


def test_certificate_manager_passes_actalis_eab_to_register_account(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[network]
stack_fqdn = "api-roborock.example.com"

[broker]
mode = "embedded"

[storage]
data_dir = "data"

[tls]
mode = "cloudflare_acme"
base_domain = "example.com"
email = "acme@example.com"
cloudflare_token_file = "secrets/cloudflare_token"
acme_server = "actalis"
acme_eab_kid_file = "secrets/acme_eab_kid"
acme_eab_hmac_key_file = "secrets/acme_eab_hmac_key"

[admin]
password_hash = "pbkdf2_sha256$600000$abc$def"
session_secret = "abcdefghijklmnopqrstuvwxyz123456"
protocol_login_email = "user@example.com"
protocol_login_pin_hash = "pbkdf2_sha256$600000$ghi$jkl"
        """.strip(),
        encoding="utf-8",
    )
    config = load_config(config_file)
    paths = resolve_paths(config_file, config)
    paths.cloudflare_token_file.parent.mkdir(parents=True, exist_ok=True)
    paths.cloudflare_token_file.write_text("cloudflare-token", encoding="utf-8")
    paths.acme_eab_kid_file.parent.mkdir(parents=True, exist_ok=True)
    paths.acme_eab_kid_file.write_text("kid-123", encoding="utf-8")
    paths.acme_eab_hmac_key_file.write_text("hmac-456", encoding="utf-8")
    manager = CertificateManager(config=config, paths=paths)
    calls: list[list[str]] = []

    def fake_run_acme(args: list[str]) -> None:
        calls.append(list(args))
        if "--install-cert" in args:
            paths.cert_file.parent.mkdir(parents=True, exist_ok=True)
            paths.key_file.parent.mkdir(parents=True, exist_ok=True)
            paths.cert_file.write_text("cert", encoding="utf-8")
            paths.key_file.write_text("key", encoding="utf-8")

    manager._run_acme = fake_run_acme  # type: ignore[method-assign]
    manager._provision_or_renew()

    assert calls[0] == [
        "--register-account",
        "-m",
        "acme@example.com",
        "--eab-kid",
        "kid-123",
        "--eab-hmac-key",
        "hmac-456",
    ]
    assert calls[1] == [
        "--issue",
        "--dns",
        "dns_cf",
        "-d",
        "api-roborock.example.com",
        "--keylength",
        "2048",
    ]
    assert calls[2][:3] == ["--install-cert", "-d", "api-roborock.example.com"]


def test_certificate_manager_rejects_missing_actalis_eab_files(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[network]
stack_fqdn = "api-roborock.example.com"

[broker]
mode = "embedded"

[storage]
data_dir = "data"

[tls]
mode = "cloudflare_acme"
base_domain = "example.com"
email = "acme@example.com"
cloudflare_token_file = "secrets/cloudflare_token"
acme_server = "actalis"
acme_eab_kid_file = "secrets/acme_eab_kid"
acme_eab_hmac_key_file = "secrets/acme_eab_hmac_key"

[admin]
password_hash = "pbkdf2_sha256$600000$abc$def"
session_secret = "abcdefghijklmnopqrstuvwxyz123456"
protocol_login_email = "user@example.com"
protocol_login_pin_hash = "pbkdf2_sha256$600000$ghi$jkl"
        """.strip(),
        encoding="utf-8",
    )
    config = load_config(config_file)
    paths = resolve_paths(config_file, config)
    paths.cloudflare_token_file.parent.mkdir(parents=True, exist_ok=True)
    paths.cloudflare_token_file.write_text("cloudflare-token", encoding="utf-8")
    manager = CertificateManager(config=config, paths=paths)

    try:
        manager._provision_or_renew()
    except RuntimeError as exc:
        assert "Actalis ACME requires EAB credentials" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError for missing Actalis EAB files")


def test_certificate_manager_issues_exact_names_for_zerossl(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[network]
stack_fqdn = "api-roborock.example.com"

[broker]
mode = "embedded"

[storage]
data_dir = "data"

[tls]
mode = "cloudflare_acme"
base_domain = "example.com"
email = "acme@example.com"
cloudflare_token_file = "secrets/cloudflare_token"
acme_server = "zerossl"

[admin]
password_hash = "pbkdf2_sha256$600000$abc$def"
session_secret = "abcdefghijklmnopqrstuvwxyz123456"
protocol_login_email = "user@example.com"
protocol_login_pin_hash = "pbkdf2_sha256$600000$ghi$jkl"
        """.strip(),
        encoding="utf-8",
    )
    config = load_config(config_file)
    paths = resolve_paths(config_file, config)
    paths.cloudflare_token_file.parent.mkdir(parents=True, exist_ok=True)
    paths.cloudflare_token_file.write_text("cloudflare-token", encoding="utf-8")
    manager = CertificateManager(config=config, paths=paths)
    calls: list[list[str]] = []

    def fake_run_acme(args: list[str]) -> None:
        calls.append(list(args))
        if "--install-cert" in args:
            paths.cert_file.parent.mkdir(parents=True, exist_ok=True)
            paths.key_file.parent.mkdir(parents=True, exist_ok=True)
            paths.cert_file.write_text("cert", encoding="utf-8")
            paths.key_file.write_text("key", encoding="utf-8")

    manager._run_acme = fake_run_acme  # type: ignore[method-assign]
    manager._provision_or_renew()

    assert calls[1] == [
        "--issue",
        "--dns",
        "dns_cf",
        "-d",
        "api-roborock.example.com",
        "-d",
        "roborock.example.com",
        "--keylength",
        "2048",
    ]
    assert calls[2][:3] == ["--install-cert", "-d", "api-roborock.example.com"]


def test_certificate_manager_keeps_wildcard_shape_when_requested(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[network]
stack_fqdn = "api-roborock.example.com"

[broker]
mode = "embedded"

[storage]
data_dir = "data"

[tls]
mode = "cloudflare_acme"
base_domain = "example.com"
email = "acme@example.com"
cloudflare_token_file = "secrets/cloudflare_token"
acme_server = "zerossl"
wildcard = true

[admin]
password_hash = "pbkdf2_sha256$600000$abc$def"
session_secret = "abcdefghijklmnopqrstuvwxyz123456"
protocol_login_email = "user@example.com"
protocol_login_pin_hash = "pbkdf2_sha256$600000$ghi$jkl"
        """.strip(),
        encoding="utf-8",
    )
    config = load_config(config_file)
    paths = resolve_paths(config_file, config)
    paths.cloudflare_token_file.parent.mkdir(parents=True, exist_ok=True)
    paths.cloudflare_token_file.write_text("cloudflare-token", encoding="utf-8")
    manager = CertificateManager(config=config, paths=paths)
    calls: list[list[str]] = []

    def fake_run_acme(args: list[str]) -> None:
        calls.append(list(args))
        if "--install-cert" in args:
            paths.cert_file.parent.mkdir(parents=True, exist_ok=True)
            paths.key_file.parent.mkdir(parents=True, exist_ok=True)
            paths.cert_file.write_text("cert", encoding="utf-8")
            paths.key_file.write_text("key", encoding="utf-8")

    manager._run_acme = fake_run_acme  # type: ignore[method-assign]
    manager._provision_or_renew()

    assert calls[1] == [
        "--issue",
        "--dns",
        "dns_cf",
        "-d",
        "example.com",
        "-d",
        "*.example.com",
        "--keylength",
        "2048",
    ]
    assert calls[2][:3] == ["--install-cert", "-d", "example.com"]


def test_certificate_manager_adds_extra_names(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[network]
stack_fqdn = "api-roborock.example.com"

[broker]
mode = "embedded"

[storage]
data_dir = "data"

[tls]
mode = "cloudflare_acme"
base_domain = "example.com"
email = "acme@example.com"
cloudflare_token_file = "secrets/cloudflare_token"
acme_server = "zerossl"
extra_names = ["Cam.Example.com", "roborock.example.com"]

[admin]
password_hash = "pbkdf2_sha256$600000$abc$def"
session_secret = "abcdefghijklmnopqrstuvwxyz123456"
protocol_login_email = "user@example.com"
protocol_login_pin_hash = "pbkdf2_sha256$600000$ghi$jkl"
        """.strip(),
        encoding="utf-8",
    )
    config = load_config(config_file)
    paths = resolve_paths(config_file, config)
    paths.cloudflare_token_file.parent.mkdir(parents=True, exist_ok=True)
    paths.cloudflare_token_file.write_text("cloudflare-token", encoding="utf-8")
    manager = CertificateManager(config=config, paths=paths)
    calls: list[list[str]] = []

    def fake_run_acme(args: list[str]) -> None:
        calls.append(list(args))
        if "--install-cert" in args:
            paths.cert_file.parent.mkdir(parents=True, exist_ok=True)
            paths.key_file.parent.mkdir(parents=True, exist_ok=True)
            paths.cert_file.write_text("cert", encoding="utf-8")
            paths.key_file.write_text("key", encoding="utf-8")

    manager._run_acme = fake_run_acme  # type: ignore[method-assign]
    manager._provision_or_renew()

    assert calls[1] == [
        "--issue",
        "--dns",
        "dns_cf",
        "-d",
        "api-roborock.example.com",
        "-d",
        "roborock.example.com",
        "-d",
        "cam.example.com",
        "--keylength",
        "2048",
    ]
    assert calls[2][:3] == ["--install-cert", "-d", "api-roborock.example.com"]


def test_run_acme_redacts_eab_credentials_in_logs_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[network]
stack_fqdn = "api-roborock.example.com"

[broker]
mode = "embedded"

[storage]
data_dir = "data"

[tls]
mode = "cloudflare_acme"
base_domain = "example.com"
email = "acme@example.com"
cloudflare_token_file = "secrets/cloudflare_token"
acme_server = "actalis"
acme_eab_kid = "kid-123"
acme_eab_hmac_key = "hmac-456"

[admin]
password_hash = "pbkdf2_sha256$600000$abc$def"
session_secret = "abcdefghijklmnopqrstuvwxyz123456"
protocol_login_email = "user@example.com"
protocol_login_pin_hash = "pbkdf2_sha256$600000$ghi$jkl"
        """.strip(),
        encoding="utf-8",
    )
    config = load_config(config_file)
    paths = resolve_paths(config_file, config)
    paths.cloudflare_token_file.parent.mkdir(parents=True, exist_ok=True)
    paths.cloudflare_token_file.write_text("cloudflare-token", encoding="utf-8")
    manager = CertificateManager(config=config, paths=paths)

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout="failed with kid-123 hmac-456 and cloudflare-token",
        )

    monkeypatch.setattr("roborock_local_server.certs.ACME_SH_PATH", tmp_path / "acme.sh")
    (tmp_path / "acme.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("roborock_local_server.certs.subprocess.run", fake_run)

    with caplog.at_level(logging.INFO, logger="roborock_local_server.certs"):
        with pytest.raises(RuntimeError) as excinfo:
            manager._run_acme(
                [
                    "--register-account",
                    "-m",
                    "acme@example.com",
                    "--eab-kid",
                    "kid-123",
                    "--eab-hmac-key",
                    "hmac-456",
                ]
            )

    combined_logs = "\n".join(caplog.messages)
    assert "kid-123" not in combined_logs
    assert "hmac-456" not in combined_logs
    assert "cloudflare-token" not in combined_logs
    assert "<redacted>" in combined_logs
    assert "kid-123" not in str(excinfo.value)
    assert "hmac-456" not in str(excinfo.value)


def test_run_acme_treats_renew_skip_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[network]
stack_fqdn = "api-roborock.example.com"

[broker]
mode = "embedded"

[storage]
data_dir = "data"

[tls]
mode = "cloudflare_acme"
base_domain = "example.com"
email = "acme@example.com"
cloudflare_token_file = "secrets/cloudflare_token"
acme_server = "zerossl"

[admin]
password_hash = "pbkdf2_sha256$600000$abc$def"
session_secret = "abcdefghijklmnopqrstuvwxyz123456"
protocol_login_email = "user@example.com"
protocol_login_pin_hash = "pbkdf2_sha256$600000$ghi$jkl"
        """.strip(),
        encoding="utf-8",
    )
    config = load_config(config_file)
    paths = resolve_paths(config_file, config)
    paths.cloudflare_token_file.parent.mkdir(parents=True, exist_ok=True)
    paths.cloudflare_token_file.write_text("cloudflare-token", encoding="utf-8")
    manager = CertificateManager(config=config, paths=paths)

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=2,
            stdout="Skipping. Next renewal time is: 2026-08-13T08:35:28Z",
        )

    monkeypatch.setattr("roborock_local_server.certs.ACME_SH_PATH", tmp_path / "acme.sh")
    (tmp_path / "acme.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("roborock_local_server.certs.subprocess.run", fake_run)

    with caplog.at_level(logging.INFO, logger="roborock_local_server.certs"):
        manager._run_acme(["--issue", "-d", "example.com"])

    assert "ACME command skipped because certificate renewal is not due" in caplog.text


def test_certificate_manager_refreshes_when_cert_domains_do_not_match_config(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[network]
stack_fqdn = "api-roborock.example.com"

[broker]
mode = "embedded"

[storage]
data_dir = "data"

[tls]
mode = "cloudflare_acme"
base_domain = "example.com"
email = "acme@example.com"
cloudflare_token_file = "secrets/cloudflare_token"
acme_server = "actalis"
acme_eab_kid = "kid-123"
acme_eab_hmac_key = "hmac-456"

[admin]
password_hash = "pbkdf2_sha256$600000$abc$def"
session_secret = "abcdefghijklmnopqrstuvwxyz123456"
protocol_login_email = "user@example.com"
protocol_login_pin_hash = "pbkdf2_sha256$600000$ghi$jkl"
        """.strip(),
        encoding="utf-8",
    )
    config = load_config(config_file)
    paths = resolve_paths(config_file, config)
    paths.cert_file.parent.mkdir(parents=True, exist_ok=True)
    paths.key_file.parent.mkdir(parents=True, exist_ok=True)
    _write_certificate(paths.cert_file, common_name="example.com", san_names=["example.com", "*.example.com"])
    paths.key_file.write_text("key", encoding="utf-8")
    manager = CertificateManager(config=config, paths=paths)
    called = {"value": False}

    def fake_provision() -> None:
        called["value"] = True

    manager._provision_or_renew = fake_provision  # type: ignore[method-assign]

    assert manager.ensure_certificate() is True
    assert called["value"] is True


def test_existing_wildcard_certificate_is_reissued_for_exact_names(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[network]
stack_fqdn = "api-roborock.example.com"

[broker]
mode = "embedded"

[storage]
data_dir = "data"

[tls]
mode = "cloudflare_acme"
base_domain = "example.com"
email = "acme@example.com"
cloudflare_token_file = "secrets/cloudflare_token"
acme_server = "zerossl"
wildcard = false

[admin]
password_hash = "pbkdf2_sha256$600000$abc$def"
session_secret = "abcdefghijklmnopqrstuvwxyz123456"
protocol_login_email = "user@example.com"
protocol_login_pin_hash = "pbkdf2_sha256$600000$ghi$jkl"
        """.strip(),
        encoding="utf-8",
    )
    config = load_config(config_file)
    paths = resolve_paths(config_file, config)
    paths.cert_file.parent.mkdir(parents=True, exist_ok=True)
    paths.key_file.parent.mkdir(parents=True, exist_ok=True)
    _write_certificate(paths.cert_file, common_name="example.com", san_names=["example.com", "*.example.com"])
    paths.key_file.write_text("key", encoding="utf-8")
    manager = CertificateManager(config=config, paths=paths)
    called = {"value": False}

    def fake_provision() -> None:
        called["value"] = True

    manager._provision_or_renew = fake_provision  # type: ignore[method-assign]

    assert manager.ensure_certificate() is True
    assert called["value"] is True


def _zerossl_manager(
    tmp_path: Path, *, wildcard: bool | None, acme_server: str = "zerossl"
) -> tuple[CertificateManager, object, list[list[str]]]:
    wildcard_line = "" if wildcard is None else f"wildcard = {'true' if wildcard else 'false'}"
    eab_lines = 'acme_eab_kid = "kid-123"\nacme_eab_hmac_key = "hmac-456"' if acme_server == "actalis" else ""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"""
[network]
stack_fqdn = "api-roborock.example.com"

[broker]
mode = "embedded"

[storage]
data_dir = "data"

[tls]
mode = "cloudflare_acme"
base_domain = "example.com"
email = "acme@example.com"
cloudflare_token_file = "secrets/cloudflare_token"
acme_server = "{acme_server}"
{wildcard_line}
{eab_lines}

[admin]
password_hash = "pbkdf2_sha256$600000$abc$def"
session_secret = "abcdefghijklmnopqrstuvwxyz123456"
protocol_login_email = "user@example.com"
protocol_login_pin_hash = "pbkdf2_sha256$600000$ghi$jkl"
        """.strip(),
        encoding="utf-8",
    )
    config = load_config(config_file)
    paths = resolve_paths(config_file, config)
    paths.cloudflare_token_file.parent.mkdir(parents=True, exist_ok=True)
    paths.cloudflare_token_file.write_text("cloudflare-token", encoding="utf-8")
    manager = CertificateManager(config=config, paths=paths)
    calls: list[list[str]] = []

    def fake_run_acme(args: list[str]) -> None:
        calls.append(list(args))
        if "--install-cert" in args:
            paths.cert_file.parent.mkdir(parents=True, exist_ok=True)
            paths.cert_file.write_text("cert", encoding="utf-8")
            paths.key_file.write_text("key", encoding="utf-8")

    manager._run_acme = fake_run_acme  # type: ignore[method-assign]
    return manager, paths, calls


def _write_acme_domain(acme_dir: Path, directory: str, domain: str, alt: str) -> Path:
    domain_dir = acme_dir / directory
    domain_dir.mkdir(parents=True)
    (domain_dir / f"{domain}.conf").write_text(f"Le_Domain='{domain}'\nLe_Alt='{alt}'\n", encoding="utf-8")
    (domain_dir / f"{domain}.key").write_text("old-key", encoding="utf-8")
    return domain_dir


def test_exact_name_issuance_removes_superseded_wildcard_certificate(tmp_path: Path) -> None:
    manager, paths, _calls = _zerossl_manager(tmp_path, wildcard=False)
    wildcard_dir = _write_acme_domain(paths.acme_dir, "example.com", "example.com", "*.example.com")
    current_dir = _write_acme_domain(
        paths.acme_dir, "api-roborock.example.com", "api-roborock.example.com", "roborock.example.com"
    )
    unrelated_dir = _write_acme_domain(paths.acme_dir, "other.example.net", "other.example.net", "no")

    manager._provision_or_renew()

    assert not wildcard_dir.exists()
    assert current_dir.exists()
    assert unrelated_dir.exists()


def test_wildcard_mode_keeps_its_certificate(tmp_path: Path) -> None:
    manager, paths, _calls = _zerossl_manager(tmp_path, wildcard=True)
    wildcard_dir = _write_acme_domain(paths.acme_dir, "example.com", "example.com", "*.example.com")

    manager._provision_or_renew()

    assert wildcard_dir.exists()


def test_failed_issuance_keeps_the_wildcard_certificate(tmp_path: Path) -> None:
    manager, paths, calls = _zerossl_manager(tmp_path, wildcard=False)
    wildcard_dir = _write_acme_domain(paths.acme_dir, "example.com", "example.com", "*.example.com")

    def failing_run_acme(args: list[str]) -> None:
        calls.append(list(args))
        if "--issue" in args:
            raise RuntimeError("ACME command failed")

    manager._run_acme = failing_run_acme  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        manager._provision_or_renew()

    assert wildcard_dir.exists()


def test_actalis_issuance_keeps_the_wildcard_certificate(tmp_path: Path) -> None:
    manager, paths, calls = _zerossl_manager(tmp_path, wildcard=None, acme_server="actalis")
    wildcard_dir = _write_acme_domain(paths.acme_dir, "example.com", "example.com", "*.example.com")

    manager._provision_or_renew()

    assert calls[1][3:5] == ["-d", "api-roborock.example.com"]
    assert wildcard_dir.exists()


def test_superseded_wildcard_deletion_is_logged_with_its_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manager, paths, _calls = _zerossl_manager(tmp_path, wildcard=False)
    wildcard_dir = _write_acme_domain(paths.acme_dir, "example.com", "example.com", "*.example.com")

    with caplog.at_level(logging.WARNING, logger="roborock_local_server.certs"):
        manager._provision_or_renew()

    assert not wildcard_dir.exists()
    assert any(
        record.levelno == logging.WARNING and "Deleted" in record.getMessage() and str(wildcard_dir) in record.getMessage()
        for record in caplog.records
    )


def test_failed_wildcard_deletion_is_not_logged_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager, paths, _calls = _zerossl_manager(tmp_path, wildcard=False)
    wildcard_dir = _write_acme_domain(paths.acme_dir, "example.com", "example.com", "*.example.com")
    monkeypatch.setattr("roborock_local_server.certs.shutil.rmtree", lambda *_args, **_kwargs: None)

    with caplog.at_level(logging.INFO, logger="roborock_local_server.certs"):
        manager._provision_or_renew()

    assert wildcard_dir.exists()
    messages = [record.getMessage() for record in caplog.records]
    assert not any("Deleted" in message or "Removed" in message for message in messages)
    assert any("Could not delete" in message and str(wildcard_dir) in message for message in messages)


def _install_certificate(paths: object, *, san_names: list[str], valid_days: int) -> bytes:
    paths.cert_file.parent.mkdir(parents=True, exist_ok=True)
    _write_certificate(paths.cert_file, common_name=san_names[0], san_names=san_names, valid_days=valid_days)
    paths.key_file.write_text("key", encoding="utf-8")
    return paths.cert_file.read_bytes()


def _fail_issue(manager: CertificateManager, calls: list[list[str]]) -> None:
    def failing_run_acme(args: list[str]) -> None:
        calls.append(list(args))
        if "--issue" in args:
            raise RuntimeError("ACME command failed")

    manager._run_acme = failing_run_acme  # type: ignore[method-assign]


def test_name_only_reissue_failure_keeps_serving_the_installed_certificate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manager, paths, calls = _zerossl_manager(tmp_path, wildcard=False)
    installed = _install_certificate(paths, san_names=["example.com", "*.example.com"], valid_days=90)
    _fail_issue(manager, calls)

    with caplog.at_level(logging.WARNING, logger="roborock_local_server.certs"):
        assert manager.ensure_certificate() is False

    assert any(call[0] == "--issue" for call in calls)
    assert paths.cert_file.read_bytes() == installed
    assert any("keeping the existing certificate" in record.getMessage() for record in caplog.records)


def test_expiring_certificate_reissue_failure_still_raises(tmp_path: Path) -> None:
    manager, paths, calls = _zerossl_manager(tmp_path, wildcard=False)
    _install_certificate(paths, san_names=["example.com", "*.example.com"], valid_days=10)
    _fail_issue(manager, calls)

    with pytest.raises(RuntimeError, match="ACME command failed"):
        manager.ensure_certificate()


def test_reissue_failure_raises_when_certificate_does_not_cover_stack_fqdn(tmp_path: Path) -> None:
    manager, paths, calls = _zerossl_manager(tmp_path, wildcard=False)
    _install_certificate(paths, san_names=["other.example.net"], valid_days=90)
    _fail_issue(manager, calls)

    with pytest.raises(RuntimeError, match="ACME command failed"):
        manager.ensure_certificate()


def test_missing_certificate_issue_failure_still_raises(tmp_path: Path) -> None:
    manager, _paths, calls = _zerossl_manager(tmp_path, wildcard=False)
    _fail_issue(manager, calls)

    with pytest.raises(RuntimeError, match="ACME command failed"):
        manager.ensure_certificate()


def test_unset_wildcard_keeps_an_installed_wildcard_certificate(tmp_path: Path) -> None:
    manager, paths, calls = _zerossl_manager(tmp_path, wildcard=None)
    _install_certificate(paths, san_names=["example.com", "*.example.com"], valid_days=90)
    wildcard_dir = _write_acme_domain(paths.acme_dir, "example.com", "example.com", "*.example.com")

    assert manager.ensure_certificate() is False
    assert calls == []

    manager._provision_or_renew()

    assert calls[1][3:7] == ["-d", "example.com", "-d", "*.example.com"]
    assert calls[2][:3] == ["--install-cert", "-d", "example.com"]
    assert wildcard_dir.exists()


def test_unset_wildcard_issues_exact_names_without_a_wildcard_certificate(tmp_path: Path) -> None:
    manager, paths, calls = _zerossl_manager(tmp_path, wildcard=None)
    _install_certificate(paths, san_names=["api-roborock.example.com"], valid_days=90)

    assert manager.ensure_certificate() is True

    assert calls[1][3:7] == ["-d", "api-roborock.example.com", "-d", "roborock.example.com"]


def test_forced_wildcard_replaces_an_exact_name_certificate(tmp_path: Path) -> None:
    manager, paths, calls = _zerossl_manager(tmp_path, wildcard=True)
    _install_certificate(paths, san_names=["api-roborock.example.com", "roborock.example.com"], valid_days=90)

    assert manager.ensure_certificate() is True

    assert calls[1][3:7] == ["-d", "example.com", "-d", "*.example.com"]


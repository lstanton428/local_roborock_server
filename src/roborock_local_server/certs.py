"""Certificate provisioning and renewal helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import shutil
import subprocess
from typing import Iterable

from cryptography import x509

from .config import AppConfig, AppPaths


LOG = logging.getLogger("roborock_local_server.certs")
ACME_SH_PATH = Path("/opt/acme.sh/acme.sh")
ACME_RENEW_SKIP_RETURN_CODE = 2


@dataclass(frozen=True)
class CertificatePaths:
    cert_file: Path
    key_file: Path


class CertificateManager:
    """Owns certificate provisioning for the release stack."""

    def __init__(self, *, config: AppConfig, paths: AppPaths) -> None:
        self.config = config
        self.paths = paths

    @property
    def certificate_paths(self) -> CertificatePaths:
        return CertificatePaths(cert_file=self.paths.cert_file, key_file=self.paths.key_file)

    def ensure_certificate(self) -> bool:
        if self.config.tls.mode == "provided":
            if not self.paths.cert_file.exists():
                raise FileNotFoundError(f"Provided TLS cert not found: {self.paths.cert_file}")
            if not self.paths.key_file.exists():
                raise FileNotFoundError(f"Provided TLS key not found: {self.paths.key_file}")
            return False
        reason = self._refresh_reason()
        if reason is None:
            return False
        try:
            self._provision_or_renew()
        except Exception as exc:
            if reason != "names":
                raise
            # The installed certificate is still valid and still covers the
            # stack hostname; only its names differ from the configuration.
            # Keep serving it instead of failing startup. The renewal loop
            # checks again later and retries the reissue.
            LOG.warning(
                "Could not reissue the TLS certificate for the configured names; "
                "keeping the existing certificate until the next renewal check: %s",
                exc,
            )
            return False
        return True

    def _needs_refresh(self) -> bool:
        return self._refresh_reason() is not None

    def _refresh_reason(self) -> str | None:
        """Return why the certificate needs issuing, or None when it is current.

        "names" means the certificate is valid and covers stack_fqdn but its names
        differ from the configuration; every other reason must be fixed to serve.
        """
        if not self.paths.cert_file.exists() or not self.paths.key_file.exists():
            return "missing"
        try:
            cert = x509.load_pem_x509_certificate(self.paths.cert_file.read_bytes())
        except Exception:
            return "missing"
        deadline = datetime.now(timezone.utc) + timedelta(days=self.config.tls.renew_days_before)
        if cert.not_valid_after_utc <= deadline:
            return "expiring"
        if self._certificate_matches_configuration(cert):
            return None
        if _names_cover(self._san_names(cert), self.config.network.stack_fqdn):
            return "names"
        return "uncovered"

    def _read_cloudflare_token(self) -> str:
        token = self.paths.cloudflare_token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise RuntimeError(f"Cloudflare token file is empty: {self.paths.cloudflare_token_file}")
        return token

    @staticmethod
    def _read_optional_secret_file(path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def _load_eab_credentials(self) -> tuple[str, str]:
        kid = self.config.tls.acme_eab_kid or self._read_optional_secret_file(self.paths.acme_eab_kid_file)
        hmac_key = self.config.tls.acme_eab_hmac_key or self._read_optional_secret_file(self.paths.acme_eab_hmac_key_file)
        if bool(kid) != bool(hmac_key):
            raise RuntimeError("ACME EAB credentials are incomplete; both KID and HMAC key are required")
        if self.config.tls.acme_server == "actalis" and not kid:
            raise RuntimeError(
                "Actalis ACME requires EAB credentials. "
                f"Checked inline config plus {self.paths.acme_eab_kid_file} and {self.paths.acme_eab_hmac_key_file}."
            )
        return kid, hmac_key

    @staticmethod
    def _redact_command(command: list[str]) -> str:
        redacted: list[str] = []
        redact_next = False
        for part in command:
            if redact_next:
                redacted.append("<redacted>")
                redact_next = False
                continue
            redacted.append(part)
            if part in {"--eab-kid", "--eab-hmac-key"}:
                redact_next = True
        return " ".join(redacted)

    @staticmethod
    def _redact_text(text: str, sensitive_values: Iterable[str]) -> str:
        redacted = text
        for value in sorted({item for item in sensitive_values if item}, key=len, reverse=True):
            redacted = redacted.replace(value, "<redacted>")
        return redacted

    @staticmethod
    def _sensitive_values(command: list[str], env: dict[str, str]) -> list[str]:
        sensitive_values: list[str] = []
        for index, part in enumerate(command[:-1]):
            if part in {"--eab-kid", "--eab-hmac-key"}:
                sensitive_values.append(command[index + 1])
        cloudflare_token = env.get("CF_Token", "").strip()
        if cloudflare_token:
            sensitive_values.append(cloudflare_token)
        return sensitive_values

    @staticmethod
    def _san_names(cert: x509.Certificate) -> set[str]:
        try:
            san_names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
                x509.DNSName
            )
        except x509.ExtensionNotFound:
            return set()
        return {name.strip().lower() for name in san_names if name.strip()}

    def _installed_certificate_names(self) -> set[str]:
        try:
            cert = x509.load_pem_x509_certificate(self.paths.cert_file.read_bytes())
        except Exception:
            return set()
        return self._san_names(cert)

    def _certificate_matches_configuration(self, cert: x509.Certificate) -> bool:
        actual_domains = self._san_names(cert)
        if not actual_domains:
            return False
        _primary_domain, issue_domains = self._certificate_domains()
        expected_domains = {domain.strip().lower() for domain in issue_domains if domain.strip()}
        return actual_domains == expected_domains

    def _run_acme(self, args: Iterable[str]) -> None:
        self.paths.acme_dir.mkdir(parents=True, exist_ok=True)
        if not ACME_SH_PATH.exists():
            raise FileNotFoundError(f"acme.sh not found in image at {ACME_SH_PATH}")
        env = dict(os.environ)
        env["CF_Token"] = self._read_cloudflare_token()
        command = [
            str(ACME_SH_PATH),
            *args,
            "--home",
            str(self.paths.acme_dir),
            "--server",
            self.config.tls.acme_server,
        ]
        display_command = self._redact_command(command)
        sensitive_values = self._sensitive_values(command, env)
        LOG.info("Running ACME command: %s", display_command)
        result = subprocess.run(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if result.stdout.strip():
            LOG.info("ACME output:\n%s", self._redact_text(result.stdout.strip(), sensitive_values))
        if result.returncode == ACME_RENEW_SKIP_RETURN_CODE:
            LOG.info("ACME command skipped because certificate renewal is not due: %s", display_command)
            return
        if result.returncode != 0:
            raise RuntimeError(f"ACME command failed ({result.returncode}): {display_command}")

    def _provision_or_renew(self) -> None:
        self.paths.certs_dir.mkdir(parents=True, exist_ok=True)
        primary_domain, issue_domains = self._certificate_domains()
        register_args = ["--register-account", "-m", self.config.tls.email]
        eab_kid, eab_hmac_key = self._load_eab_credentials()
        if eab_kid and eab_hmac_key:
            register_args.extend(
                [
                    "--eab-kid",
                    eab_kid,
                    "--eab-hmac-key",
                    eab_hmac_key,
                ]
            )
        self._run_acme(register_args)
        issue_args = ["--issue", "--dns", "dns_cf"]
        for domain in issue_domains:
            issue_args.extend(["-d", domain])
        issue_args.extend(["--keylength", "2048"])
        self._run_acme(
            issue_args
        )
        self._run_acme(
            [
                "--install-cert",
                "-d",
                primary_domain,
                "--fullchain-file",
                str(self.paths.cert_file),
                "--key-file",
                str(self.paths.key_file),
            ]
        )
        if not self.paths.cert_file.exists() or not self.paths.key_file.exists():
            raise RuntimeError("ACME completed without writing certificate files")
        if self.config.tls.acme_server == "zerossl" and not any(domain.startswith("*.") for domain in issue_domains):
            self._remove_superseded_wildcard_certificate(primary_domain)

    def _remove_superseded_wildcard_certificate(self, primary_domain: str) -> None:
        """Delete acme.sh's copy of a wildcard certificate that exact names replaced.

        Releases before tls.wildcard issued base_domain plus *.base_domain. Once the
        exact-name certificate is installed, that key is no longer used, so do not
        keep a key for every subdomain on disk until it expires.
        """
        base_domain = self.config.tls.base_domain
        if not base_domain or base_domain == primary_domain:
            return
        for directory_name in (base_domain, f"{base_domain}_ecc"):
            domain_dir = self.paths.acme_dir / directory_name
            conf_path = domain_dir / f"{base_domain}.conf"
            try:
                conf_text = conf_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if f"*.{base_domain}" not in conf_text:
                continue
            shutil.rmtree(domain_dir, ignore_errors=True)
            if domain_dir.exists():
                LOG.warning(
                    "Could not delete the superseded wildcard certificate and key for %s; remove %s manually",
                    base_domain,
                    domain_dir,
                )
            else:
                LOG.warning("Deleted the superseded wildcard certificate and key for %s at %s", base_domain, domain_dir)

    def _certificate_domains(self) -> tuple[str, list[str]]:
        stack_fqdn = self.config.network.stack_fqdn
        if self.config.tls.acme_server == "actalis":
            return stack_fqdn, [stack_fqdn]
        if self._use_wildcard():
            base_domain = self.config.tls.base_domain
            return base_domain, [base_domain, f"*.{base_domain}"]
        # Cover only the names the stack serves: the API hostname and the
        # token.r hostname the vacuum is given (the API name without "api-"),
        # which camera bootstrap uses on some models. A wildcard would let a
        # leaked key impersonate every subdomain of the user's domain.
        names = [stack_fqdn]
        token_host = stack_fqdn.removeprefix("api-")
        if token_host and "." in token_host:
            names.append(token_host)
        names.extend(self.config.tls.extra_names)
        return stack_fqdn, list(dict.fromkeys(names))

    def _use_wildcard(self) -> bool:
        if self.config.tls.wildcard is not None:
            return self.config.tls.wildcard
        # Unset means auto: keep the base_domain plus *.base_domain shape that
        # releases before tls.wildcard issued, so camera aliases under
        # base_domain keep working after an upgrade. Other installs, including
        # new ones, get exact names.
        base_domain = self.config.tls.base_domain
        if not base_domain:
            return False
        return self._installed_certificate_names() == {base_domain, f"*.{base_domain}"}


def _names_cover(names: set[str], hostname: str) -> bool:
    """Return whether certificate DNS names cover hostname, including one-label wildcards."""
    hostname = hostname.strip().lower()
    if hostname in names:
        return True
    _label, _dot, parent = hostname.partition(".")
    return bool(parent) and f"*.{parent}" in names

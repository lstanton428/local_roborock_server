from pathlib import Path

import pytest
from conftest import write_release_config

from roborock_local_server.config import load_config, resolve_paths


def test_logging_defaults_and_overrides(tmp_path):
    path = write_release_config(tmp_path)
    defaults = load_config(path).logging
    assert defaults.max_bytes == 10 * 1024 * 1024
    assert defaults.backup_count == 3
    path.write_text(path.read_text() + '\n[logging]\nmax_size_mb=2\nbackup_count=0\n')
    config = load_config(path)
    assert config.logging.max_bytes == 2 * 1024 * 1024
    assert config.logging.backup_count == 0


@pytest.mark.parametrize("field,value", [
    ("max_size_mb", "0"), ("max_size_mb", "-1"), ("max_size_mb", "1025"),
    ("max_size_mb", "1.5"), ("max_size_mb", "true"), ("max_size_mb", '"10"'),
    ("backup_count", "-1"), ("backup_count", "21"), ("backup_count", "false"),
])
def test_logging_rejects_invalid_limits(tmp_path, field, value):
    path = write_release_config(tmp_path)
    path.write_text(path.read_text() + f'\n[logging]\n{field}={value}\n')
    with pytest.raises(ValueError, match=f"logging.{field} must be an integer"):
        load_config(path)


def test_ingress_port_is_off_unless_configured(tmp_path):
    path = write_release_config(tmp_path)
    assert load_config(path).network.ingress_port == 0
    text = path.read_text()
    path.write_text(text.replace("[network]\n", "[network]\ningress_port = 8099\n", 1))
    assert load_config(path).network.ingress_port == 8099
    for value in ("-1", "65536"):
        path.write_text(text.replace("[network]\n", f"[network]\ningress_port = {value}\n", 1))
        with pytest.raises(ValueError, match="network.ingress_port"):
            load_config(path)


def test_turn_config_defaults_and_rejects_unsafe_provided_credentials(tmp_path):
    path = write_release_config(tmp_path)
    base = path.read_text()
    path.write_text(base + '\n[turn]\nmode="provided"\nhost="relay.example"\npassword="secret"\n')
    config = load_config(path)
    assert config.turn.realm == "relay.example"
    assert config.turn.username == "roborock"
    path.write_text(base + '\n[turn]\nmode="provided"\nhost="relay.example"\npassword="bad:secret"\n')
    with pytest.raises(ValueError, match="turn.password cannot"):
        load_config(path)


def test_turn_denied_peer_ips_is_optional_and_validated(tmp_path):
    path = write_release_config(tmp_path)
    base = path.read_text()
    turn = '\n[turn]\nmode="provided"\nhost="relay.example"\npassword="secret"\n'
    path.write_text(base + turn)
    assert load_config(path).turn.denied_peer_ips == ()
    path.write_text(base + turn + 'denied_peer_ips=["10.0.0.0/8", "192.168.50.7"]\n')
    assert load_config(path).turn.denied_peer_ips == ("10.0.0.0/8", "192.168.50.7")
    path.write_text(base + turn + 'denied_peer_ips=["not-an-ip"]\n')
    with pytest.raises(ValueError, match="turn.denied_peer_ips"):
        load_config(path)


def test_turn_config_mode_is_case_insensitive(tmp_path):
    path = write_release_config(tmp_path)
    path.write_text(path.read_text() + '\n[turn]\nmode="Provided"\nhost="relay.example"\npassword="secret"\n')
    config = load_config(path)
    assert config.turn.mode == "provided"
    assert config.turn.username == "roborock"


def test_turn_config_legacy_enabled_flag_maps_to_external(tmp_path):
    path = write_release_config(tmp_path)
    path.write_text(path.read_text() + '\n[turn]\nenabled=true\nhost="relay.example"\nusername="user"\npassword="secret"\n')
    assert load_config(path).turn.mode == "external"


def test_turn_config_rejects_nonpositive_ttl(tmp_path):
    path = write_release_config(tmp_path)
    path.write_text(path.read_text() + '\n[turn]\nmode="external"\nhost="relay.example"\nusername="user"\npassword="secret"\nttl=0\n')
    with pytest.raises(ValueError, match="turn.ttl must"):
        load_config(path)


def test_broker_topic_acl_defaults_to_audit_and_is_case_insensitive(tmp_path):
    path = write_release_config(tmp_path)
    assert load_config(path).broker.topic_acl == "audit"
    path.write_text(path.read_text().replace("[broker]\n", '[broker]\ntopic_acl = "Enforce"\n'))
    assert load_config(path).broker.topic_acl == "enforce"


def test_broker_topic_acl_rejects_unknown_mode(tmp_path):
    path = write_release_config(tmp_path)
    path.write_text(path.read_text().replace("[broker]\n", '[broker]\ntopic_acl = "strict"\n'))
    with pytest.raises(ValueError, match="broker.topic_acl must be one of: off, audit, enforce"):
        load_config(path)


def test_load_config_and_resolve_paths(tmp_path: Path) -> None:
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
mode = "provided"
cert_file = "certs/fullchain.pem"
key_file = "certs/privkey.pem"

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

    assert config.network.stack_fqdn == "api-roborock.example.com"
    assert config.network.https_port == 555
    assert config.network.mqtt_tls_port == 8881
    assert config.network.advertised_https_port == 555
    assert config.network.advertised_mqtt_tls_port == 8881
    assert config.admin.protocol_auth_enabled is True
    assert config.admin.new_connections_enabled is True
    assert config.admin.protocol_login_email == "user@example.com"
    assert paths.data_dir == (tmp_path / "data").resolve()
    assert paths.cert_file == (tmp_path / "certs" / "fullchain.pem").resolve()
    assert paths.key_file == (tmp_path / "certs" / "privkey.pem").resolve()


def test_load_config_requires_protocol_login_credentials(tmp_path: Path) -> None:
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
mode = "provided"
cert_file = "certs/fullchain.pem"
key_file = "certs/privkey.pem"

[admin]
password_hash = "pbkdf2_sha256$600000$abc$def"
session_secret = "abcdefghijklmnopqrstuvwxyz123456"
        """.strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="admin.protocol_login_email is required"):
        load_config(config_file)


def test_load_config_requires_api_prefix_for_stack_fqdn(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[network]
stack_fqdn = "lashleyhomeassist.duckdns.org"

[broker]
mode = "embedded"

[storage]
data_dir = "data"

[tls]
mode = "provided"
cert_file = "certs/fullchain.pem"
key_file = "certs/privkey.pem"

[admin]
password_hash = "pbkdf2_sha256$600000$abc$def"
session_secret = "abcdefghijklmnopqrstuvwxyz123456"
protocol_login_email = "user@example.com"
protocol_login_pin_hash = "pbkdf2_sha256$600000$ghi$jkl"
        """.strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="network.stack_fqdn must start with api-"):
        load_config(config_file)


def test_load_config_normalizes_stack_fqdn_and_validates_cloudflare_base_domain(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[network]
stack_fqdn = "https://API-Roborock.Example.com:8443/path"

[broker]
mode = "embedded"

[storage]
data_dir = "data"

[tls]
mode = "cloudflare_acme"
base_domain = "https://Example.com/path"
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

    assert config.network.stack_fqdn == "api-roborock.example.com"
    assert config.tls.base_domain == "example.com"


def test_load_config_requires_actalis_eab(tmp_path: Path) -> None:
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

[admin]
password_hash = "pbkdf2_sha256$600000$abc$def"
session_secret = "abcdefghijklmnopqrstuvwxyz123456"
protocol_login_email = "user@example.com"
protocol_login_pin_hash = "pbkdf2_sha256$600000$ghi$jkl"
        """.strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Actalis requires"):
        load_config(config_file)


def test_load_config_accepts_actalis_eab_file_paths(tmp_path: Path) -> None:
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
    assert config.tls.acme_server == "actalis"
    assert config.tls.acme_eab_kid == ""
    assert config.tls.acme_eab_hmac_key == ""
    assert paths.acme_eab_kid_file == (tmp_path / "secrets" / "acme_eab_kid").resolve()
    assert paths.acme_eab_hmac_key_file == (tmp_path / "secrets" / "acme_eab_hmac_key").resolve()


def test_load_config_accepts_legacy_inline_actalis_eab(tmp_path: Path) -> None:
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
    assert config.tls.acme_eab_kid == "kid-123"
    assert config.tls.acme_eab_hmac_key == "hmac-456"


def test_load_config_normalizes_mixed_case_actalis(tmp_path: Path) -> None:
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
acme_server = "Actalis"
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
    assert config.tls.acme_server == "actalis"


def test_load_config_rejects_invalid_ports(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[network]
stack_fqdn = "api-roborock.example.com"
https_port = 70000

[broker]
mode = "embedded"

[storage]
data_dir = "data"

[tls]
mode = "provided"
cert_file = "certs/fullchain.pem"
key_file = "certs/privkey.pem"

[admin]
password_hash = "pbkdf2_sha256$600000$abc$def"
session_secret = "abcdefghijklmnopqrstuvwxyz123456"
protocol_login_email = "user@example.com"
protocol_login_pin_hash = "pbkdf2_sha256$600000$ghi$jkl"
        """.strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="network.https_port must be between 1 and 65535"):
        load_config(config_file)


def test_load_config_accepts_reverse_proxy_network_settings(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[network]
stack_fqdn = "api-roborock.example.com"
https_port = 555
mqtt_tls_port = 8881
advertised_https_port = 443
advertised_mqtt_tls_port = 8883

[broker]
mode = "embedded"

[storage]
data_dir = "data"

[tls]
mode = "provided"
cert_file = "certs/fullchain.pem"
key_file = "certs/privkey.pem"

[admin]
password_hash = "pbkdf2_sha256$600000$abc$def"
session_secret = "abcdefghijklmnopqrstuvwxyz123456"
protocol_login_email = "user@example.com"
protocol_login_pin_hash = "pbkdf2_sha256$600000$ghi$jkl"
        """.strip(),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.network.https_port == 555
    assert config.network.mqtt_tls_port == 8881
    assert config.network.advertised_https_port == 443
    assert config.network.advertised_mqtt_tls_port == 8883
    assert config.network.listener_mode == "local_tls"
    assert config.network.trusted_proxies == ("127.0.0.1", "::1")


def test_load_config_external_tls_requires_no_certificate_material(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[network]
stack_fqdn = "api-roborock.example.com"
listener_mode = "external_tls"
https_port = 555
mqtt_tls_port = 8881

[broker]
mode = "embedded"

[storage]
data_dir = "data"

[tls]
mode = "provided"

[admin]
password_hash = "pbkdf2_sha256$600000$abc$def"
session_secret = "abcdefghijklmnopqrstuvwxyz123456"
protocol_login_email = "user@example.com"
protocol_login_pin_hash = "pbkdf2_sha256$600000$ghi$jkl"
        """.strip(),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.network.listener_mode == "external_tls"
    assert config.tls.cert_file == ""
    assert config.tls.key_file == ""


def test_load_config_rejects_unknown_listener_mode(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[network]
stack_fqdn = "api-roborock.example.com"
listener_mode = "passthrough"

[broker]
mode = "embedded"

[storage]
data_dir = "data"

[tls]
mode = "provided"
cert_file = "certs/fullchain.pem"
key_file = "certs/privkey.pem"

[admin]
password_hash = "pbkdf2_sha256$600000$abc$def"
session_secret = "abcdefghijklmnopqrstuvwxyz123456"
protocol_login_email = "user@example.com"
protocol_login_pin_hash = "pbkdf2_sha256$600000$ghi$jkl"
        """.strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="listener_mode must be 'local_tls' or 'external_tls'"):
        load_config(config_file)


def test_load_config_rejects_external_tls_with_cloudflare_acme(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[network]
stack_fqdn = "api-roborock.example.com"
listener_mode = "external_tls"

[broker]
mode = "embedded"

[storage]
data_dir = "data"

[tls]
mode = "cloudflare_acme"
base_domain = "example.com"
email = "user@example.com"
cloudflare_token_file = "secrets/cf_token"

[admin]
password_hash = "pbkdf2_sha256$600000$abc$def"
session_secret = "abcdefghijklmnopqrstuvwxyz123456"
protocol_login_email = "user@example.com"
protocol_login_pin_hash = "pbkdf2_sha256$600000$ghi$jkl"
        """.strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="external_tls.*requires tls.mode='provided'"):
        load_config(config_file)


_TRUSTED_PROXIES_CONFIG = """
[network]
stack_fqdn = "api-roborock.example.com"
trusted_proxies = {trusted_proxies}

[broker]
mode = "embedded"

[storage]
data_dir = "data"

[tls]
mode = "provided"
cert_file = "certs/fullchain.pem"
key_file = "certs/privkey.pem"

[admin]
password_hash = "pbkdf2_sha256$600000$abc$def"
session_secret = "abcdefghijklmnopqrstuvwxyz123456"
protocol_login_email = "user@example.com"
protocol_login_pin_hash = "pbkdf2_sha256$600000$ghi$jkl"
"""


def test_load_config_accepts_trusted_proxies(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        _TRUSTED_PROXIES_CONFIG.format(trusted_proxies='["10.42.0.0/16", "10.1.1.10", "fd00::/8"]').strip(),
        encoding="utf-8",
    )

    assert load_config(config_file).network.trusted_proxies == ("10.42.0.0/16", "10.1.1.10", "fd00::/8")


def test_load_config_rejects_invalid_trusted_proxy(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(_TRUSTED_PROXIES_CONFIG.format(trusted_proxies='["traefik"]').strip(), encoding="utf-8")

    with pytest.raises(ValueError, match="network.trusted_proxies entry 'traefik'"):
        load_config(config_file)


def test_admin_allowed_networks_parse_and_validate(tmp_path: Path) -> None:
    config_file = write_release_config(tmp_path)
    text = config_file.read_text(encoding="utf-8").replace(
        "[admin]\n", '[admin]\nallowed_networks = ["192.168.8.0/24", "100.64.0.0/10"]\n', 1
    )
    config_file.write_text(text, encoding="utf-8")
    assert load_config(config_file).admin.allowed_networks == ("192.168.8.0/24", "100.64.0.0/10")

    config_file.write_text(text.replace('"192.168.8.0/24"', '"not-a-network"'), encoding="utf-8")
    with pytest.raises(ValueError, match="admin.allowed_networks"):
        load_config(config_file)


def _with_tls_lines(path: Path, lines: str) -> Path:
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('[tls]\nmode = "provided"\n', f'[tls]\nmode = "provided"\n{lines}\n'), encoding="utf-8")
    return path


def test_tls_wildcard_is_unset_unless_configured(tmp_path: Path) -> None:
    path = write_release_config(tmp_path)
    assert load_config(path).tls.wildcard is None
    _with_tls_lines(path, "wildcard = false")
    assert load_config(path).tls.wildcard is False


@pytest.mark.parametrize("name", ["*.example.com", "*.cam.example.com"])
def test_tls_extra_names_rejects_wildcards(tmp_path: Path, name: str) -> None:
    path = _with_tls_lines(write_release_config(tmp_path), f'extra_names = ["cam.example.com", "{name}"]')
    with pytest.raises(ValueError, match="tls.extra_names cannot contain wildcard names"):
        load_config(path)


def test_admin_allowed_networks_rejects_an_explicit_empty_list(tmp_path: Path) -> None:
    config_file = write_release_config(tmp_path)
    text = config_file.read_text(encoding="utf-8")
    config_file.write_text(text.replace("[admin]\n", "[admin]\nallowed_networks = []\n", 1), encoding="utf-8")
    with pytest.raises(ValueError, match=r"admin.allowed_networks is empty.*0\.0\.0\.0/0"):
        load_config(config_file)

    config_file.write_text(
        text.replace("[admin]\n", '[admin]\nallowed_networks = ["0.0.0.0/0", "::/0"]\n', 1), encoding="utf-8"
    )
    assert load_config(config_file).admin.allowed_networks == ("0.0.0.0/0", "::/0")

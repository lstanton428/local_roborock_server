import re
import tomllib
from pathlib import Path

from packaging.version import Version
import yaml

from roborock_local_server import __version__

# The fork's add-on and image are versioned <package version>-hardened.<n>.
FORK_SUFFIX = re.compile(r"-hardened\.\d+$")
FORK_IMAGE = "ghcr.io/lstanton428/local_roborock_server"


def test_package_version_matches_pyproject() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == __version__


def test_init_module_exports_single_version_literal() -> None:
    init_text = Path("src/roborock_local_server/__init__.py").read_text(encoding="utf-8")
    matches = re.findall(r'__version__\s*=\s*"([^"]+)"', init_text)
    assert matches == [__version__]


def test_home_assistant_addon_version_matches_package_version() -> None:
    addon = yaml.safe_load(Path("roborock_local_server_addon/config.yaml").read_text(encoding="utf-8"))
    assert FORK_SUFFIX.search(addon["version"])
    assert FORK_SUFFIX.sub("", addon["version"]) == __version__


def test_stable_addon_never_selects_a_prerelease_image() -> None:
    addon = yaml.safe_load(Path("roborock_local_server_addon/config.yaml").read_text(encoding="utf-8"))
    version = Version(FORK_SUFFIX.sub("", addon["version"]))
    assert not version.is_prerelease and not version.is_devrelease
    assert addon["slug"] == "roborock_local_server"
    assert addon["image"] == FORK_IMAGE


def test_stable_addon_mounts_only_its_own_config_folder() -> None:
    addon = yaml.safe_load(Path("roborock_local_server_addon/config.yaml").read_text(encoding="utf-8"))
    # /ssl holds Home Assistant's own certificate and key, and all_addon_configs
    # exposes every other add-on's config and secrets.
    assert addon["map"] == ["addon_config:rw"]


def test_fork_release_workflow_publishes_the_addon_image() -> None:
    workflow = Path(".github/workflows/docker-release.yml").read_text(encoding="utf-8")
    assert f"IMAGE_NAME: {FORK_IMAGE.removeprefix('ghcr.io/')}" in workflow
    repository = yaml.safe_load(Path("repository.yaml").read_text(encoding="utf-8"))
    assert repository["url"] == "https://github.com/lstanton428/local_roborock_server"


def test_addon_repository_lists_only_the_hardened_addon() -> None:
    # Home Assistant lists every add-on folder in the repository. The fork ships
    # one add-on; upstream's Beta add-on pulled upstream's old 1.1.0-rc2 image.
    addons = sorted(path.parent.name for path in Path(".").glob("*/config.yaml"))
    assert addons == ["roborock_local_server_addon"]


def test_addon_turn_defaults_to_disabled_without_reserving_udp_ports() -> None:
    addon = yaml.safe_load(Path("roborock_local_server_addon/config.yaml").read_text(encoding="utf-8"))
    assert addon["schema"]["turn_mode"] == "list(disabled|provided|external)?"
    assert addon["options"]["turn_mode"] == "disabled"
    assert addon["ports"]["3478/udp"] is None
    assert all(addon["ports"][f"{port}/udp"] is None for port in range(49160, 49180))

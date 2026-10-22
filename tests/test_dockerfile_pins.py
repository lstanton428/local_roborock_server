"""Static checks that the Docker image's inputs stay pinned.

The Dockerfile pins base images by digest, acme.sh by tag plus SHA-256, and
every Python package (including the hatchling build backend) by hash. These
tests parse the Dockerfile and .dockerignore so a later edit cannot quietly
drop one of those pins.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
BUILD_REQUIREMENTS = "build-requirements.txt"

PINNED_IMAGE = re.compile(r"\S+@sha256:[0-9a-f]{64}")
PROJECT_TARGETS = {"/app", "/app/", "."}


def _instructions() -> list[tuple[str, str]]:
    """Return (KEYWORD, arguments) pairs with line continuations joined."""
    instructions: list[tuple[str, str]] = []
    pending = ""
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        full = (pending + line).strip()
        pending = ""
        keyword, _, args = full.partition(" ")
        instructions.append((keyword.upper(), args.strip()))
    if pending.strip():
        keyword, _, args = pending.strip().partition(" ")
        instructions.append((keyword.upper(), args.strip()))
    return instructions


def _run_commands() -> list[str]:
    # Drop RUN's own flags (--mount=..., --network=...) so only the shell command remains.
    return [
        re.sub(r"^(?:--\S+\s+)+", "", args) for keyword, args in _instructions() if keyword == "RUN"
    ]


def _shell_segments() -> list[list[str]]:
    """Split every RUN into simple commands (on &&, ||, ; and |) as token lists."""
    segments: list[list[str]] = []
    for command in _run_commands():
        for part in re.split(r"&&|\|\||;|\|", command):
            tokens = shlex.split(part)
            if tokens:
                segments.append(tokens)
    return segments


def _pip_installs() -> list[list[str]]:
    installs = []
    for tokens in _shell_segments():
        # Skip leading VAR=value environment assignments.
        words = [t for t in tokens if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", t)] or tokens
        for index, word in enumerate(words[:-1]):
            if word.rsplit("/", 1)[-1] in {"pip", "pip3"} and words[index + 1] == "install":
                installs.append(words[index + 2 :])
                break
            if word in {"python", "python3"} and words[index + 1 : index + 4] == ["-m", "pip", "install"]:
                installs.append(words[index + 4 :])
                break
    return installs


def _project_installs() -> list[list[str]]:
    return [args for args in _pip_installs() if PROJECT_TARGETS.intersection(args)]


def test_every_from_line_is_pinned_by_digest() -> None:
    from_lines = [args for keyword, args in _instructions() if keyword == "FROM"]
    assert from_lines, "Dockerfile has no FROM instruction"
    images = [next(word for word in args.split() if not word.startswith("--")) for args in from_lines]
    unpinned = [image for image in images if not PINNED_IMAGE.fullmatch(image)]
    assert not unpinned, f"FROM lines without an @sha256: digest: {unpinned}"


def test_acme_sh_is_downloaded_by_tag_and_checksummed() -> None:
    acme_runs = [cmd for cmd in _run_commands() if "acmesh-official/acme.sh" in cmd]
    assert len(acme_runs) == 1, "expected exactly one RUN that downloads acme.sh"
    command = acme_runs[0]
    assert "refs/heads/" not in command, "acme.sh must not be fetched from a branch"
    assert "/archive/refs/tags/" in command, "acme.sh must be fetched from a release tag"
    download_at = command.index("acmesh-official/acme.sh")
    check_at = command.find("sha256sum -c")
    assert check_at > download_at, "acme.sh download must be followed by `sha256sum -c`"
    extract_at = command.find("tar ")
    assert extract_at > check_at, "acme.sh must be verified before it is extracted"

    args = dict(
        args.split("=", 1) for keyword, args in _instructions() if keyword == "ARG" and "=" in args
    )
    assert re.fullmatch(r"[0-9a-f]{64}", args.get("ACME_SH_SHA256", "")), "ACME_SH_SHA256 must be a SHA-256"


def test_requirement_file_pip_installs_require_hashes() -> None:
    requirement_installs = [args for args in _pip_installs() if "-r" in args or "--requirement" in args]
    assert requirement_installs, "expected pip to install from hash-pinned requirement files"
    for args in requirement_installs:
        assert "--require-hashes" in args, f"pip install without --require-hashes: {args}"

    # The only pip install allowed without hashes is the local project itself.
    others = [args for args in _pip_installs() if args not in requirement_installs]
    assert others == _project_installs(), f"unexpected unhashed pip installs: {others}"

    # The hatchling build backend must come from the hashed build requirements.
    assert any(BUILD_REQUIREMENTS in " ".join(args) for args in requirement_installs), (
        f"{BUILD_REQUIREMENTS} must be installed with --require-hashes"
    )


def test_project_install_pulls_in_nothing_unpinned() -> None:
    project_installs = _project_installs()
    assert len(project_installs) == 1, f"expected one pip install of the project, got {project_installs}"
    args = project_installs[0]
    assert "--no-deps" in args, "project install must not resolve dependencies"
    assert "--no-build-isolation" in args, "project install must not fetch an unpinned build backend"


def test_uv_export_refuses_a_stale_lock() -> None:
    exports = [t for t in _shell_segments() if t[:2] == ["uv", "export"]]
    assert exports, "expected `uv export` to produce the hashed requirements from uv.lock"
    for tokens in exports:
        assert "--locked" in tokens, "uv export must use --locked so a stale uv.lock fails the build"
        assert "--frozen" not in tokens, "--frozen silently ignores a stale uv.lock"


def test_dockerignore_reincludes_lock_and_build_requirements() -> None:
    lines = {line.strip() for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()}
    assert "!uv.lock" in lines
    assert f"!{BUILD_REQUIREMENTS}" in lines


def test_build_requirements_are_fully_hashed_and_match_build_backend() -> None:
    requirements: list[str] = []
    for line in (REPO_ROOT / BUILD_REQUIREMENTS).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace() and requirements:
            requirements[-1] += " " + line.strip()
        else:
            requirements.append(line.strip())
    assert requirements, f"{BUILD_REQUIREMENTS} is empty"
    pins = {}
    for requirement in requirements:
        match = re.match(r"([A-Za-z0-9._-]+)==(\S+)", requirement)
        assert match, f"unpinned build requirement: {requirement!r}"
        assert "--hash=sha256:" in requirement, f"build requirement without a hash: {match.group(1)}"
        pins[match.group(1).lower()] = match.group(2)

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    backend_requires = pyproject["build-system"]["requires"]
    for requirement in backend_requires:
        name = re.match(r"[A-Za-z0-9._-]+", requirement).group(0).lower()
        assert name in pins, f"build backend requirement {name} missing from {BUILD_REQUIREMENTS}"


@pytest.mark.parametrize("path", ["Dockerfile", ".dockerignore", BUILD_REQUIREMENTS, "uv.lock"])
def test_pinned_inputs_exist(path: str) -> None:
    assert (REPO_ROOT / path).is_file()

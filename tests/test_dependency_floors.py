"""Runtime security floors must survive in project and installed wheel metadata."""

import tomllib
from importlib.metadata import requires
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


@pytest.mark.parametrize("source", ["project", "installed"])
@pytest.mark.parametrize(
    ("package", "minimum"),
    [
        ("aiohttp", "3.14.4"),
        ("anyio", "4.14.2"),
        ("brotli", "1.2.1"),
        ("certifi", "2026.10.19"),
        ("filelock", "4.2.1"),
        ("multidict", "6.9.1"),
        ("pillow", "13.0.0"),
        ("soupsieve", "2.9.0"),
        ("urllib3", "2.8.0"),
    ],
)
def test_runtime_metadata_keeps_security_floor(source: str, package: str, minimum: str) -> None:
    # Independent minimums from scraper#38, not from the current lock or declarations.
    if source == "project":
        manifest = Path(__file__).resolve().parents[1] / "pyproject.toml"
        dependencies = tomllib.loads(manifest.read_text())["project"]["dependencies"]
    else:
        dependencies = requires("dungle-scrubs-scraper") or []

    requirements = [Requirement(dependency) for dependency in dependencies]
    assert any(
        canonicalize_name(requirement.name) == package
        and requirement.marker is None
        and any(
            specifier.operator in {">=", ">"} and Version(specifier.version) >= Version(minimum)
            for specifier in requirement.specifier
        )
        for requirement in requirements
    ), f"{source} metadata must require {package}>={minimum} unconditionally"

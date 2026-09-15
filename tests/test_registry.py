"""Tests for registry loading and validation."""

from pathlib import Path

import pytest

from atomic_l0g.registry import REPO_TIERS, load_registry, validate_registry

REPO_SOURCES = Path(__file__).resolve().parents[1] / "sources"

EXPECTED_PROJECTS = {
    "flatcar",
    "azure-container-linux",
    "azure-linux",
    "bottlerocket",
    "amazon-linux",
    "talos",
    "kairos",
    "universal-blue",
    "opensuse-microos",
    "rancher-elemental",
    "aws-blogs",
    "coreos",
    "rhcos",
    "bootc",
}


@pytest.fixture(scope="module")
def registry():
    return load_registry(REPO_SOURCES)


def test_bundled_registry_is_valid(registry):
    assert validate_registry(registry) == []


def test_expected_projects_present(registry):
    assert EXPECTED_PROJECTS <= set(registry.projects)


def test_every_repository_has_a_known_tier(registry):
    assert registry.repo_tiers
    for repo, tier in registry.repo_tiers.items():
        assert tier in REPO_TIERS, f"{repo} has tier {tier!r}"


def test_repo_project_inversion(registry):
    assert registry.repo_project["flatcar/Flatcar"] == "flatcar"
    assert registry.repo_project["microsoft/azure-container-linux"] == "azure-container-linux"
    assert registry.repo_project["fedora/bootc/base-images"] == "bootc"


def test_both_providers_are_declared(registry):
    providers = {
        provider
        for project in registry.projects.values()
        for provider, _repo in project.repositories()
    }
    assert providers == {"github", "gitlab"}


def test_bots_and_themes_are_loaded(registry):
    assert registry.bots["github"]
    assert registry.bots["gitlab"]
    assert "bootc" in registry.themes
    assert registry.security_labels


def test_missing_distros_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="distros.yaml"):
        load_registry(tmp_path)


def test_untiered_repository_is_reported(tmp_path):
    (tmp_path / "distros.yaml").write_text(
        "example:\n  repos:\n    github: [example/thing]\n", encoding="utf-8"
    )
    (tmp_path / "repos.yaml").write_text("tiers:\n  core: []\n", encoding="utf-8")

    problems = validate_registry(load_registry(tmp_path))

    assert any("no tier" in problem for problem in problems)


def test_orphan_tier_is_reported(tmp_path):
    (tmp_path / "distros.yaml").write_text(
        "example:\n  repos:\n    github: [example/thing]\n", encoding="utf-8"
    )
    (tmp_path / "repos.yaml").write_text(
        "tiers:\n  core: [example/thing]\n  watch: [ghost/repo]\n", encoding="utf-8"
    )

    problems = validate_registry(load_registry(tmp_path))

    assert any("ghost/repo" in problem and "not declared" in problem for problem in problems)


def test_unknown_provider_is_reported(tmp_path):
    (tmp_path / "distros.yaml").write_text(
        "example:\n  repos:\n    sourcehut: [example/thing]\n", encoding="utf-8"
    )

    problems = validate_registry(load_registry(tmp_path))

    assert any("unknown provider" in problem for problem in problems)


def test_duplicate_repository_is_reported(tmp_path):
    (tmp_path / "distros.yaml").write_text(
        "one:\n  repos:\n    github: [example/thing]\n"
        "two:\n  repos:\n    github: [example/thing]\n",
        encoding="utf-8",
    )
    (tmp_path / "repos.yaml").write_text("tiers:\n  core: [example/thing]\n", encoding="utf-8")

    problems = validate_registry(load_registry(tmp_path))

    assert any("already declared" in problem for problem in problems)


def test_empty_project_is_reported(tmp_path):
    (tmp_path / "distros.yaml").write_text("empty:\n  vendor: Nobody\n", encoding="utf-8")

    problems = validate_registry(load_registry(tmp_path))

    assert any("declares no repos" in problem for problem in problems)


def test_non_http_feed_is_reported(tmp_path):
    (tmp_path / "distros.yaml").write_text(
        "example:\n  feeds:\n    blog: ftp://example.org/feed.xml\n", encoding="utf-8"
    )

    problems = validate_registry(load_registry(tmp_path))

    assert any("not an http(s) URL" in problem for problem in problems)

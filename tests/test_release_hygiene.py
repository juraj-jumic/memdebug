"""The files a release depends on must agree with each other, so a release cannot go out half-updated."""
import re
from pathlib import Path

import pytest

import memdebug

ROOT = Path(__file__).resolve().parent.parent
needs_repo = pytest.mark.skipif(not (ROOT / "pyproject.toml").exists(), reason="not running from a source checkout")


def text(name):
    return (ROOT / name).read_text(encoding="utf-8")


@needs_repo
def test_the_version_is_the_same_everywhere_and_has_a_changelog_entry():
    declared = re.search(r'^version = "(.+)"', text("pyproject.toml"), re.M).group(1)
    assert declared == memdebug.__version__
    assert re.search(rf"^## \[{re.escape(declared)}\]", text("CHANGELOG.md"), re.M), "the changelog has no entry for this version"


@needs_repo
def test_the_licence_files_exist_and_name_the_author():
    assert "Apache License" in text("LICENSE") and "Version 2.0" in text("LICENSE")
    assert "Juraj Jumić" in text("NOTICE") and "Apache License, Version 2.0" in text("NOTICE")
    assert 'license = "Apache-2.0"' in text("pyproject.toml") and "Juraj Jumić" in text("pyproject.toml")


@needs_repo
def test_every_link_between_the_documents_points_at_a_file_that_exists():
    broken = []
    for name in ["README.md", "ROADMAP.md", "SECURITY.md", "CONTRIBUTING.md", "CHANGELOG.md", *[f"docs/{p.name}" for p in (ROOT / "docs").glob("*.md")]]:
        for target in re.findall(r"\]\(([^)\s]+)\)", text(name)):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            if not ((ROOT / Path(name).parent / target.split("#")[0]).exists()):
                broken.append((name, target))
    assert broken == []


@needs_repo
def test_no_document_still_carries_a_placeholder_that_would_be_published():
    for name in ["README.md", "ROADMAP.md", "SECURITY.md", "CONTRIBUTING.md", "CHANGELOG.md", "pyproject.toml"]:
        body = text(name)
        assert not re.search(r"your-username|<you>|TODO|FIXME|lorem ipsum", body, re.I), name


@needs_repo
def test_the_workflows_ask_for_the_least_they_need():
    def code(name):  # the instructions only: a comment explaining that no password is used must not count as using one
        return "\n".join(line for line in text(name).splitlines() if not line.lstrip().startswith("#"))

    ci, release = code(".github/workflows/ci.yml"), code(".github/workflows/release.yml")
    assert re.search(r"^permissions:\s*\n\s+contents: read", ci, re.M) and re.search(r"^permissions:\s*\n\s+contents: read", release, re.M)
    assert "id-token" not in ci
    # Only two jobs may mint an identity token: publish (to PyPI) and github-release (for the attestation of the stand-alone programs), both after the build.
    assert release.count("id-token: write") == 2 and release.index("id-token: write") > release.index("  publish:")
    # Only github-release may write to the repository (it creates the release), and nothing may do so before publish has succeeded.
    assert release.count("contents: write") == 1 and release.index("contents: write") > release.index("  github-release:")
    assert release.count("attestations: write") == 1 and release.index("attestations: write") > release.index("  github-release:")
    assert re.search(r'tags: \["v\*"\]', release) and "pull_request" not in release  # a pull request can never publish
    assert "environment: pypi" in release and "password" not in release.lower() and "secrets." not in release


@needs_repo
def test_the_workflows_are_valid_yaml_with_the_expected_jobs():
    yaml = pytest.importorskip("yaml")
    ci = yaml.safe_load(text(".github/workflows/ci.yml"))
    assert {"test", "lint", "package", "standalone"} <= set(ci["jobs"])
    release = yaml.safe_load(text(".github/workflows/release.yml"))
    jobs = release["jobs"]
    assert set(jobs) == {"build", "verify", "standalone", "publish", "github-release"} and jobs["verify"]["needs"] == "build"
    assert set(jobs["publish"]["needs"]) == {"build", "verify", "standalone"}  # nothing is published unless the hand-over was re-checked and every stand-alone build passed
    assert set(jobs["github-release"]["needs"]) == {"publish", "standalone"}  # the GitHub release comes only after PyPI has the release
    assert jobs["publish"]["if"] == jobs["github-release"]["if"] == "github.ref_type == 'tag'"  # a manual dry run publishes nothing and releases nothing
    assert jobs["publish"]["environment"] == "pypi" and "environment" not in jobs["github-release"]
    assert yaml.safe_load(text(".github/dependabot.yml"))["version"] == 2


@needs_repo
def test_the_ignore_files_keep_private_data_and_clutter_out_of_the_repository(tmp_path):
    import os
    import shutil
    import subprocess

    if not shutil.which("git"):
        pytest.skip("git is not installed")
    shutil.copy(ROOT / ".gitignore", tmp_path / ".gitignore")
    shutil.copy(ROOT / ".gitattributes", tmp_path / ".gitattributes")
    private = [".venv/lib/x.py", "webui-copy.db", "ledger.db", "ledger.db-wal", "stores.json", "copies/open-webui/webui.db", "memdebug-report.md", "copies/open-webui/notes.bin", "src/memdebug/__pycache__/notes.txt",
               "src/memdebug/__pycache__/a.cpython-312.pyc", ".pytest_cache/v", "src/memdebug.egg-info/PKG-INFO", "dist/x.whl", ".env", "id.pem",
               "backups/notes/20261007-s1/files/a.md", "session.jsonl", "projects/-p1/abc.jsonl"]
    wanted = ["src/memdebug/cli.py", "tests/test_x.py", "docs/threat-model.md", "pyproject.toml", "LICENSE", "NOTICE", ".github/workflows/ci.yml", "README.md"]
    for name in private + wanted:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=env, capture_output=True)
    listed = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=tmp_path, check=True, env=env, capture_output=True, text=True).stdout
    shown = {line[3:] for line in listed.splitlines()}
    assert shown >= set(wanted) and not shown & set(private), sorted(shown & set(private))


@needs_repo
def test_no_database_settings_file_or_cache_is_part_of_the_source_tree():
    skip = {".venv", ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", "memdebug.egg-info"}
    found = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and not skip & set(p.relative_to(ROOT).parts)
             and (p.suffix in {".db", ".sqlite", ".sqlite3", ".pem", ".key"} or p.name in {"stores.json", ".env"})]
    assert found == []


@needs_repo
def test_the_published_package_points_back_at_the_repository():
    body = text("pyproject.toml")
    urls = re.search(r"\[project\.urls\]\n(.*?)\n\n", body, re.S).group(1)
    assert 'Source = "https://github.com/juraj-jumic/memdebug"' in urls and "Issues = " in urls and "Homepage = " in urls
    assert "https://github.com/juraj-jumic/memdebug/security/advisories/new" in text("SECURITY.md")


@needs_repo
def test_one_change_starts_one_run_and_a_dry_run_can_never_publish():
    ci, release = text(".github/workflows/ci.yml"), text(".github/workflows/release.yml")
    assert "branches: [main]" in ci and "cancel-in-progress: true" in ci and "workflow_dispatch" in ci
    assert "workflow_dispatch" in release
    publish = release[release.index("  publish:"):]
    assert "if: github.ref_type == 'tag'" in publish.split("steps:")[0]  # the job itself is skipped unless a tag started the run
    assert "if: github.ref_type == 'tag'" in release[:release.index("  publish:")]  # and the tag-versus-version check only runs for tags


@needs_repo
def test_dependabot_groups_its_updates_so_paired_actions_move_together():
    yaml = pytest.importorskip("yaml")
    updates = yaml.safe_load(text(".github/dependabot.yml"))["updates"]
    assert {u["package-ecosystem"] for u in updates} == {"github-actions", "pip"}
    assert all(u.get("groups") for u in updates)
    actions = next(u for u in updates if u["package-ecosystem"] == "github-actions")
    assert "*" in next(iter(actions["groups"].values()))["patterns"]


@needs_repo
def test_the_upload_and_download_actions_move_together_and_only_trusted_owners_are_used():
    release = text(".github/workflows/release.yml")
    downloads = set(re.findall(r"actions/download-artifact@(\S+)", release))
    assert len(downloads) == 1 and release.count("actions/download-artifact@") == 3  # verify, publish and github-release use the very same version
    for name in (".github/workflows/ci.yml", ".github/workflows/release.yml"):
        for owner, action, ref in re.findall(r"uses:\s*([\w.-]+)/([\w./-]+)@(\S+)", text(name)):
            assert owner in {"actions", "pypa"}, f"{name} uses an action from {owner}"
            assert ref not in {"main", "master", "latest"}, f"{name} uses {action} at a moving reference"

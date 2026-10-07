# Releasing memdebug

What the repository already does, and the few steps only the owner can do (they need accounts and credentials).

## Already in place

* `ci.yml` runs the tests and the self-check on Linux, Windows and macOS, lints and type-checks, and **builds and installs the package** from
  scratch on every push, so a broken package is noticed before a release.
* `release.yml` builds and publishes when you push a tag like `v0.2.0`. It uses PyPI **trusted publishing**: no password or token is stored
  anywhere; PyPI trusts this one workflow in this one repository.
* `dependabot.yml` proposes updates to the pinned actions and to dependencies every week.
* A test (`tests/test_release_hygiene.py`) fails if the version, the changelog, the licence files or the links in the documentation drift apart.
* The name `memdebug` was free on PyPI when last checked. Nothing is reserved until you publish: check again before you do.

## Once, before the first release

1. Create the GitHub repository and push this code. Keep the `.github` folder.
2. In the repository settings, turn on **private vulnerability reporting** (Settings, Code security). `SECURITY.md` points people there.
3. The project's addresses are already in `pyproject.toml` (`[project.urls]`), pointing at https://github.com/juraj-jumic/memdebug.
4. On PyPI, add a **pending trusted publisher** (Your account, Publishing): owner `juraj-jumic`, repository `memdebug`, workflow `release.yml`,
   environment `pypi`. In the GitHub repository create the environment `pypi` (Settings, Environments); you can require your approval there.
5. Optional but wise: do the whole thing once on TestPyPI first.

## Try the release workflow without publishing

In the repository: Actions, **release**, **Run workflow**, pick any branch. It builds the package, runs PyPI's metadata check and uploads the
files as an artifact; a second job downloads them again the way publishing would, re-checks them and installs the wheel in a clean
environment. Then it stops: the publish step only runs for a version tag, and only after both jobs pass. Do this after changing the workflows (or when Dependabot
proposes new versions of the upload and download actions) so a problem shows up on a quiet day, not on release day.

## Each release

1. Run `memdebug selftest` and the tests on your own machine (Windows is the one CI cannot fully stand in for).
2. Update `__version__` in `src/memdebug/__init__.py` and `version` in `pyproject.toml` (they must match) and rename the changelog's `[Unreleased]` heading to the new
   version with the release date, for example `## [0.2.1] - 2026-11-02`.
3. Commit, then tag and push: `git tag v0.2.0 && git push origin v0.2.0`. The workflow refuses to publish if the tag and the version differ.
4. Approve the `pypi` environment if you required that. After a minute, in a clean virtual environment (not your working one), check the real
   install: `pip install memdebug==<version>`, then `memdebug --version` and `memdebug demo`. Do this on Windows as well as Linux or macOS.

## If something goes wrong

* A published version cannot be changed or reused. Fix forward with a new version; you can "yank" a bad one on PyPI.
* If the workflow cannot publish, nothing is lost: the built files are attached to the run and can be inspected.

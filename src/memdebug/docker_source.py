"""Reading Open WebUI's database out of its Docker container, so you never have to copy it by hand.

Docker can do far more than memdebug needs, so this module does exactly three things and nothing else:

  1. `docker ps` to find running containers whose image is Open WebUI;
  2. `docker exec CONTAINER python -c <fixed script>` to ask the container's own Python to take a consistent snapshot of
     `webui.db` with SQLite's backup function, opened READ-ONLY, into a temporary file in the container's /tmp;
  3. `docker cp` to bring that file out, then `docker exec CONTAINER rm -f` to remove the temporary file.

Commands are argument lists (no shell). The container name comes from Docker's own output, so it is untrusted: it must match
a strict pattern (which also stops it from being mistaken for a Docker option). Output, time and file size are limited, the
copy must be a real SQLite file, and it replaces the previous copy atomically, so a failed refresh never destroys the last
good one. Open WebUI's data is never modified.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from .adapters.markdown_git import _spawn_flags
from .errors import MemdebugError
from .textsafe import safe_text

CONTAINER_DB = "/app/backend/data/webui.db"
CONTAINER_TMP = "/tmp/memdebug-webui-snapshot.db"
MAX_COPY_BYTES = 4 * 1024**3
MAX_OUTPUT = 1_000_000
MAX_CONTAINERS = 20
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_ENV_KEYS = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
             "PROGRAMFILES", "XDG_RUNTIME_DIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY")

BACKUP_SCRIPT = (
    "import sqlite3\n"
    f"source = sqlite3.connect('file:{CONTAINER_DB}?mode=ro', uri=True)\n"
    f"target = sqlite3.connect('{CONTAINER_TMP}')\n"
    "source.backup(target)\n"
    "target.close()\n"
    "source.close()\n"
)


class DockerError(MemdebugError):
    """Docker is missing, not running, or the container is not what was expected."""


def valid_container(name: object) -> bool:
    """Whether `name` is a string that matches the strict container-name pattern."""
    return isinstance(name, str) and bool(_NAME.match(name))


def find_docker() -> str | None:
    """Docker on PATH, ignoring the current folder (a planted docker.exe there must never be run)."""
    names = ["docker.exe"] if os.name == "nt" else ["docker"]
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        directory = directory.strip().strip('"')
        if not directory or not os.path.isabs(directory):
            continue
        for name in names:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


class Docker:
    """The docker command-line client, run without a shell and with limits."""

    def __init__(self, path: str | None = None):
        found = path or find_docker()
        if found is None:
            raise DockerError("Docker was not found. Is Docker Desktop installed and running?")
        self.path = found

    def run(self, args: list[str], *, timeout: float) -> tuple[int, bytes, str]:
        """Run docker with `args` and wait for it.

        The command gets no shell, no standard input, a small allow-listed environment and the temporary folder as its working
        folder.

        Returns:
            The exit code, the standard output as bytes, and the first 2000 bytes of standard error decoded as text.

        Raises:
            DockerError: If docker does not finish within `timeout` seconds, cannot be started, or writes more than `MAX_OUTPUT`
                bytes to standard output.
        """
        env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
        try:
            done = subprocess.run([self.path, *args], capture_output=True, timeout=timeout, env=env, shell=False,
                                  stdin=subprocess.DEVNULL, cwd=tempfile.gettempdir(), **_spawn_flags())
        except subprocess.TimeoutExpired:
            raise DockerError(f"Docker did not answer within {timeout:g} seconds") from None
        except OSError as exc:
            raise DockerError(f"Docker could not be run ({exc.strerror})") from exc
        if len(done.stdout) > MAX_OUTPUT:
            raise DockerError("Docker produced unexpectedly large output")
        return done.returncode, done.stdout, done.stderr[:2000].decode("utf-8", "replace")


def _explain(error: str) -> str:
    text = error.lower()
    if "cannot connect to the docker daemon" in text or "error during connect" in text or "is the docker daemon running" in text:
        return "Docker is not running. Start Docker Desktop and try again."
    if "no such container" in text or "is not running" in text:
        return "the Open WebUI container is not running"
    return safe_text(error.strip() or "unknown error", 200)


def list_open_webui(docker: Docker) -> list[str]:
    """Names of running containers whose image is Open WebUI."""
    code, out, err = docker.run(["ps", "--format", "{{.Names}}\t{{.Image}}"], timeout=15)
    if code != 0:
        raise DockerError(_explain(err))
    found: list[str] = []
    for line in out.decode("utf-8", "replace").splitlines()[:200]:
        name, _, image = line.partition("\t")
        if valid_container(name) and "open-webui" in image.lower() and name not in found:
            found.append(name)
        if len(found) >= MAX_CONTAINERS:
            break
    return found


def copy_database(docker: Docker, container: str, destination: Path) -> None:
    """Replace `destination` with a fresh, consistent, read-only copy of the container's webui.db."""
    if not valid_container(container):
        raise DockerError("that is not a usable container name")
    try:
        info = os.lstat(destination)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise DockerError("the copy's location must be a plain file, not a link or a folder")
    except FileNotFoundError:
        pass
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    try:
        partial.unlink()
    except FileNotFoundError:
        pass
    try:
        for interpreter in ("python", "python3"):
            code, _, err = docker.run(["exec", container, interpreter, "-c", BACKUP_SCRIPT], timeout=120)
            if code == 0:
                break
            if "executable file not found" not in err and "not found in $path" not in err.lower():
                break
        if code != 0:
            if "unable to open database file" in err or "no such file" in err.lower():
                raise DockerError(f"no SQLite database was found at {CONTAINER_DB} in that container (a different data folder, or Open WebUI uses PostgreSQL)")
            raise DockerError("could not take the snapshot: " + _explain(err))
        code, _, err = docker.run(["cp", f"{container}:{CONTAINER_TMP}", str(partial)], timeout=600)
        if code != 0:
            raise DockerError("could not copy the snapshot out: " + _explain(err))
        try:
            info = os.lstat(partial)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_COPY_BYTES:
                raise DockerError("the copy is not a file of reasonable size")
            with open(partial, "rb") as handle:
                if handle.read(16) != b"SQLite format 3\0":
                    raise DockerError("what came out of the container is not a SQLite database")
        except OSError as exc:
            raise DockerError(f"the copy could not be checked ({exc.strerror})") from exc
        os.replace(partial, destination)
    finally:
        try:
            docker.run(["exec", container, "rm", "-f", CONTAINER_TMP], timeout=30)
        except DockerError:
            pass
        try:
            partial.unlink()
        except OSError:
            pass

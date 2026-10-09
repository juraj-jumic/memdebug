"""The stores memdebug watches: a small settings file, finding likely stores, and opening them.

The settings file is read back into the program, so it is validated strictly (names, kinds, option types, size) and a
damaged or hostile file produces a clear message, never a crash or a surprise. Finding likely stores only looks at
folder names and whether they hold markdown files; it never reads their contents.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import stat
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import longpath
from .adapters.base import MemoryAdapter
from .adapters.folder import FolderAdapter
from .adapters.markdown_git import MarkdownGitAdapter, _is_reparse_point, _valid_relpath
from .adapters.mem0 import Mem0Adapter, build_mem0_memory, validate_scope
from .adapters.openwebui import OpenWebUIAdapter
from .docker_source import Docker, copy_database, list_open_webui, valid_container
from .errors import MemdebugError
from .textsafe import has_unsafe_chars, safe_text

KINDS = ("markdown", "folder", "mem0", "openwebui")
KIND_NAMES = {"markdown": "markdown notes in a git repository", "folder": "a folder of markdown notes",
              "mem0": "self-hosted Mem0", "openwebui": "Open WebUI's memory"}
MAX_SETTINGS_BYTES = 1_000_000
MAX_STORES = 200
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}\Z")
_OPTIONS = ("subdir", "user_id", "agent_id", "run_id", "docker", "files")


class SettingsError(MemdebugError):
    """The settings file is unusable or a store's settings are wrong."""


@dataclass(frozen=True)
class StoreConfig:
    """The settings of one watched store, as kept in the settings file.

    Attributes:
        name: The store's name, which must pass `valid_name`.
        kind: One of `KINDS`.
        path: Where the store is: a folder, or a database file for Open WebUI and Mem0.
        subdir: A subfolder to watch, for the markdown and folder kinds.
        user_id: The user to read, for Open WebUI and Mem0 (required for Mem0).
        agent_id: Mem0 only: the agent to read.
        run_id: Mem0 only: the run to read.
        docker: Open WebUI only: the container its database is copied from, before every look.
        files: Folder only: watch just these files in it (comma-separated names), nothing else in the folder.
    """

    name: str
    kind: str
    path: str
    subdir: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    docker: str | None = None  # Open WebUI only: the container its database is copied from, before every look
    files: str | None = None  # folder only: watch just these files in it (comma-separated names), nothing else in the folder

    def describe(self) -> str:
        """The store's name followed by its kind in words, for showing to the person."""
        return f"{self.name} ({KIND_NAMES[self.kind]})"


@dataclass
class Registry:
    """The list of watched stores, as held in the settings file.

    Attributes:
        stores: The watched stores, with unique names.
        witness: The path of the witness file, if one is set.
    """

    stores: list[StoreConfig] = field(default_factory=list)
    witness: str | None = None

    def get(self, name: str) -> StoreConfig | None:
        """The store with this name, or None."""
        return next((s for s in self.stores if s.name == name), None)

    def add(self, store: StoreConfig) -> None:
        """Add a store.

        Raises:
            SettingsError: If a store with that name already exists or there are already `MAX_STORES` stores.
        """
        if self.get(store.name) is not None:
            raise SettingsError(f"there is already a store called {safe_text(store.name, 40)}; choose another name or remove it first")
        if len(self.stores) >= MAX_STORES:
            raise SettingsError("too many stores")
        self.stores.append(store)

    def remove(self, name: str) -> StoreConfig:
        """Remove the store with this name and return it.

        Raises:
            SettingsError: If no store has that name.
        """
        store = self.get(name)
        if store is None:
            raise SettingsError(f"no store is called {safe_text(name, 40)}")
        self.stores.remove(store)
        return store


def valid_name(name: str) -> bool:
    """Whether `name` is a usable store name.

    It must be 1 to 40 lowercase letters, digits, dots, dashes or underscores, starting with a letter or digit.
    """
    return bool(_NAME.match(name))


def _text(value: object, what: str, *, limit: int = 1000) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or has_unsafe_chars(value) or "\0" in value:
        raise SettingsError(f"the settings file has an invalid {what}")
    return value


def validate_store(data: object) -> StoreConfig:
    """Check one store entry from the settings file and build its `StoreConfig`.

    The entry must be a dict with exactly the known fields: `name`, `kind` and `path` are required and the rest are optional.
    Mem0 stores need a `user_id`, `files` is only for folder stores and `docker` only for Open WebUI stores.

    Raises:
        SettingsError: If anything about the entry is missing, unknown, too long, unsafe or of the wrong type.
    """
    if not isinstance(data, dict) or not {"name", "kind", "path"} <= set(data) or set(data) - {"name", "kind", "path", *_OPTIONS}:
        raise SettingsError("the settings file has a store entry with unexpected fields")
    name, kind = data["name"], data["kind"]
    if not isinstance(name, str) or not valid_name(name):
        raise SettingsError("a store name must be 1 to 40 lowercase letters, digits, dots, dashes or underscores")
    if kind not in KINDS:
        raise SettingsError(f"unknown store type for {safe_text(name, 40)}")
    options = {key: (None if data.get(key) is None else _text(data[key], key, limit=256)) for key in _OPTIONS}
    if kind == "mem0" and options["user_id"] is None:
        raise SettingsError(f"the Mem0 store {name} needs a user id")
    if options["files"] is not None:
        names = options["files"].split(",")
        if kind != "folder" or len(names) > 10 or any("/" in n or _valid_relpath(n, (".md",)) is None for n in names):
            raise SettingsError(f"{safe_text(name, 40)}: 'files' must be 1 to 10 plain markdown file names, for a folder store")
    if options["docker"] is not None and (kind != "openwebui" or not valid_container(options["docker"])):
        raise SettingsError(f"{safe_text(name, 40)}: a container can only be named for an Open WebUI store, with a plain container name")
    return StoreConfig(name=name, kind=kind, path=_text(data["path"], "path"), **options)


def load_registry(path: Path) -> Registry:
    """The registry from its settings file; an empty one if there is no file yet."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return Registry()
    except OSError as exc:
        raise SettingsError(f"cannot read the settings file ({exc.strerror})") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SETTINGS_BYTES:
        raise SettingsError("the settings file is not a plain file of reasonable size")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SettingsError(f"the settings file is damaged ({safe_text(exc, 80)}); fix or delete {safe_text(path, 120)}") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or set(data) - {"version", "stores", "witness"}:
        raise SettingsError("the settings file is not in a format this version understands")
    raw_stores = data.get("stores", [])
    if not isinstance(raw_stores, list) or len(raw_stores) > MAX_STORES:
        raise SettingsError("the settings file lists too many stores")
    registry = Registry()
    for item in raw_stores:
        registry.add(validate_store(item))
    witness = data.get("witness")
    registry.witness = None if witness is None else _text(witness, "witness path")
    return registry


def save_registry(registry: Registry, path: Path) -> None:
    """Write the settings atomically and privately."""
    document = {"version": 1, "stores": [{k: v for k, v in asdict(s).items() if v is not None} for s in registry.stores]}
    if registry.witness:
        document["witness"] = registry.witness
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write((json.dumps(document, indent=2, ensure_ascii=True) + "\n").encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise SettingsError(f"cannot save the settings file ({exc.strerror})") from exc


# -- opening a store ----------------------------------------------------------------------------------------------------

@dataclass
class OpenedStore:
    """A store opened for reading.

    Attributes:
        adapter: The read-only reader for the store.
        scope: The scope the store's records are kept under in the ledger.
        notes: Messages for the person about how the store was opened.
    """

    adapter: MemoryAdapter
    scope: dict[str, str]
    notes: list[str] = field(default_factory=list)


def open_store(cfg: StoreConfig, *, refresh: bool = True) -> OpenedStore:
    """Open a store for reading.

    An Open WebUI store that lives in Docker gets a fresh copy of its database first (`refresh=False` reads the copy that is
    already there, for commands that only look).
    """
    if cfg.docker and refresh:
        copy_database(Docker(), cfg.docker, Path(cfg.path))
    if cfg.kind == "markdown":
        adapter = MarkdownGitAdapter(cfg.path, store=cfg.name, subdir=cfg.subdir)
        return OpenedStore(adapter, {"store": adapter.store})
    if cfg.kind == "folder":
        folder = FolderAdapter(cfg.path, store=cfg.name, subdir=cfg.subdir, only=tuple(cfg.files.split(",")) if cfg.files else None)
        return OpenedStore(folder, {"store": folder.store})
    if cfg.kind == "openwebui":
        webui = OpenWebUIAdapter(cfg.path, user_id=cfg.user_id)
        return OpenedStore(webui, webui.scope)
    scope = validate_scope({k: v for k, v in (("user_id", cfg.user_id), ("agent_id", cfg.agent_id), ("run_id", cfg.run_id)) if v is not None})
    memory, notes = build_mem0_memory(None)
    return OpenedStore(Mem0Adapter(memory, Path(cfg.path)), scope, notes)


# -- telling what a path is -------------------------------------------------------------------------------------------

def detect_kind(path: Path) -> str:
    """markdown, folder, openwebui or mem0, from what the path is. Reads structure only, never contents."""
    try:
        info = longpath.stat(path)
    except OSError as exc:
        raise SettingsError(f"cannot find that path ({exc.strerror})") from exc
    if stat.S_ISDIR(info.st_mode):
        return "markdown" if longpath.exists(path / ".git") else "folder"
    if not stat.S_ISREG(info.st_mode):
        raise SettingsError("that is neither a folder nor a file")
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=3)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "memory" in tables:
                columns = {row[1] for row in db.execute('PRAGMA table_info("memory")')}
                if {"id", "user_id", "content"} <= columns:
                    return "openwebui"
            if "history" in tables:
                return "mem0"
    except sqlite3.Error:
        pass
    raise SettingsError("that file is not a database memdebug recognises (Open WebUI's webui.db or Mem0's history.db)")


# -- finding likely stores ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """A store that could be watched, found on this computer but not yet added.

    Attributes:
        kind: One of `KINDS`.
        name: The suggested store name.
        path: Where the store is.
        why: A short description of what it is, shown to the person.
        docker: For Open WebUI in Docker, the name of the container.
        files: For a folder, the only files in it that should be watched.
    """

    kind: str
    name: str
    path: Path
    why: str
    docker: str | None = None
    files: tuple[str, ...] | None = None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def discover(home: Path | None = None) -> list[Candidate]:
    """Stores that are probably on this computer. Looks only at folder names and for markdown files, never inside them."""
    base = (home or Path.home()) / ".claude" / "projects"
    found: list[Candidate] = []
    taken: set[str] = set()
    try:
        entries = sorted(os.scandir(longpath.fs(base)), key=lambda e: e.name)[:300]
    except OSError:
        return []
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False) or has_unsafe_chars(entry.name):
                continue
            memory = base / entry.name / "memory"  # the ordinary form: it is what gets stored and shown
            info = longpath.lstat(memory)
            if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
                continue
            names = longpath.listdir(memory)[:2000]
        except OSError:
            continue
        if not any(n.lower().endswith(".md") for n in names):
            continue
        slug = "claude-" + (_slug(entry.name)[-24:].strip("-") or "project")
        name, n = slug[:40], 2
        while name in taken:
            name, n = f"{slug[:36]}-{n}", n + 1
        taken.add(name)
        found.append(Candidate("folder", name, memory, "Claude Code's memory notes for one project"))
        if len(found) >= 50:
            break
    return found


def discover_docker(copies_dir: Path, docker: Docker | None = None) -> list[Candidate]:
    """Open WebUI containers that are running right now. Quietly nothing if Docker is missing or not running: it is only a convenience."""
    try:
        client = docker or Docker()
        names = list_open_webui(client)
    except MemdebugError:
        return []
    found = []
    for container in names:
        slug = _slug(container)[:40] or "open-webui"
        found.append(Candidate("openwebui", slug, copies_dir / slug / "webui.db", f"Open WebUI's memory, running in Docker as '{container}'", container))
    return found

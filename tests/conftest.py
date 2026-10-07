import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

DDL = (
    "CREATE TABLE history (id TEXT PRIMARY KEY, memory_id TEXT, old_memory TEXT, new_memory TEXT, "
    "event TEXT, created_at DATETIME, updated_at DATETIME, is_deleted INTEGER, actor_id TEXT, role TEXT)"
)
BASE = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)
NOW = BASE + timedelta(hours=12)


def iso(minutes: float = 0) -> str:
    return (BASE + timedelta(minutes=minutes)).isoformat()


def raw_row(path, **row):
    """Insert a history row exactly as given, bypassing any validation."""
    values = {
        "id": str(uuid.uuid4()), "memory_id": "m1", "old_memory": None, "new_memory": None,
        "event": "ADD", "created_at": iso(0), "updated_at": iso(0), "is_deleted": 0,
        "actor_id": None, "role": None,
    }
    values.update(row)
    conn = sqlite3.connect(path)
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    conn.execute(f"INSERT INTO history ({cols}) VALUES ({marks})", list(values.values()))
    conn.commit()
    conn.close()


class FakeMemory:
    """Stands in for mem0.Memory: same get_all/history shapes, history in a real SQLite file
    laid out like Mem0 2.2.1."""

    def __init__(self, history_path):
        self.path = str(history_path)
        conn = sqlite3.connect(self.path)
        conn.execute(DDL)
        conn.commit()
        conn.close()
        self.live: dict[str, dict] = {}
        self.created: dict[str, str] = {}
        self.get_all_calls: list[dict] = []
        self.after_get_all = None  # one-shot hook, runs after a listing is taken

    def api_add(self, memory_id, text, minute, user="u1"):
        self.live[memory_id] = {"memory": text, "user_id": user}
        self.created[memory_id] = iso(minute)
        raw_row(self.path, memory_id=memory_id, new_memory=text, event="ADD",
                created_at=iso(minute), updated_at=iso(minute))

    def api_update(self, memory_id, text, minute):
        old = self.live[memory_id]["memory"]
        self.live[memory_id]["memory"] = text
        raw_row(self.path, memory_id=memory_id, old_memory=old, new_memory=text, event="UPDATE",
                created_at=self.created[memory_id], updated_at=iso(minute))

    def api_delete(self, memory_id, minute):
        old = self.live.pop(memory_id)["memory"]
        raw_row(self.path, memory_id=memory_id, old_memory=old, event="DELETE",
                created_at=self.created[memory_id], updated_at=iso(minute), is_deleted=1)

    def sneaky_edit(self, memory_id, text):
        self.live[memory_id]["memory"] = text  # no history row, as with a direct storage edit

    def sneaky_delete(self, memory_id):
        del self.live[memory_id]

    def get_all(self, *, filters=None, top_k=20, show_expired=False, **kwargs):
        self.get_all_calls.append({"filters": filters, "top_k": top_k, "show_expired": show_expired})
        items = [
            {"id": mid, **data} for mid, data in self.live.items()
            if all(data.get(k) == v for k, v in (filters or {}).items())
        ]
        result = {"results": items[:top_k]}
        if self.after_get_all is not None:
            hook, self.after_get_all = self.after_get_all, None
            hook()
        return result

    def history(self, memory_id):
        conn = sqlite3.connect(self.path)
        rows = conn.execute(
            "SELECT id, memory_id, old_memory, new_memory, event, created_at, updated_at, is_deleted, "
            "actor_id, role FROM history WHERE memory_id = ? ORDER BY created_at ASC, DATETIME(updated_at) ASC",
            (memory_id,),
        ).fetchall()
        conn.close()
        keys = ["id", "memory_id", "old_memory", "new_memory", "event", "created_at", "updated_at",
                "is_deleted", "actor_id", "role"]
        return [dict(zip(keys, r, strict=False)) for r in rows]


@pytest.fixture
def fake(tmp_path):
    return FakeMemory(tmp_path / "history.db")


MAX_TEST_ID = 1000


def pytest_collection_modifyitems(items):
    """Refuse test names that Windows cannot handle.

    pytest stores the running test's name in an environment variable, and Windows rejects values over
    32,767 characters. Parametrizing on a huge string without short ids passes on Linux and crashes on
    Windows, so catch it everywhere.
    """
    too_long = [item.nodeid[:80] + "..." for item in items if len(item.nodeid) > MAX_TEST_ID]
    if too_long:
        raise pytest.UsageError(
            f"{len(too_long)} test name(s) are over {MAX_TEST_ID} characters (give parametrize explicit ids=...): "
            + "; ".join(too_long[:3])
        )


@pytest.fixture(autouse=True)
def no_real_docker(monkeypatch):
    """No test may talk to the Docker on the machine it runs on: a developer's own containers would change the results (and be touched).
    Tests that need Docker replace this with a fake themselves."""
    import memdebug.docker_source as docker_source
    import memdebug.stores as stores

    def refuse(path=None):
        raise docker_source.DockerError("Docker was not found (tests never use the real one)")

    monkeypatch.setattr(stores, "Docker", refuse)


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path_factory):
    """Every test gets an empty home folder and data folder of its own, so none can scan the developer's real agents or write to their
    real ledger folder. Tests that need a particular home set it themselves."""
    home = tmp_path_factory.mktemp("home")
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))

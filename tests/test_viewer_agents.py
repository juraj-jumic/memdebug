"""The viewer's Agents page: a report only. Folder names are hostile text, the one command it shows is fixed, and it works without the ledger."""
import hashlib
import re

import pytest

from memdebug.agents import Agent, FoundAgent
from memdebug.errors import LedgerError
from memdebug.stores import Candidate
from memdebug.viewer import server as server_module
from test_viewer import XSS, Viewer, make_ledger, start


def digest(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude/projects/-p1/memory").mkdir(parents=True)
    (home / ".claude/projects/-p1/memory/MEMORY.md").write_text("index\n", encoding="utf-8")
    (home / ".gemini").mkdir()
    (home / ".gemini/GEMINI.md").write_text("## Gemini Added Memories\n- likes tea\n", encoding="utf-8")
    (home / ".gemini/oauth_creds.json").write_text("TOP-SECRET-GEMINI", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def viewer(tmp_path, home):
    server, thread = start(make_ledger(tmp_path))
    yield Viewer(server, tmp_path)
    server.shutdown()
    server.server_close()
    thread.join(5)


def hostile_agent():
    place = Candidate("folder", XSS, "/h/" + XSS, XSS, files=(XSS,))
    return [FoundAgent(Agent("evil", XSS, (".x",), XSS), [place])]


def test_the_agents_page_lists_what_is_found_and_changes_nothing(viewer, home):
    before = digest(home)
    status, headers, body = viewer.get("/agents")
    assert status == 200 and headers["content-type"].startswith("text/html")
    assert "Claude Code" in body and "Gemini CLI" in body and "GEMINI.md" in body and "memdebug setup" in body
    assert "oauth_creds" not in body and "TOP-SECRET" not in body and "likes tea" not in body  # no credentials, no memory text
    assert digest(home) == before


def test_every_page_links_to_the_agents_page(viewer):
    assert 'href="/agents"' in viewer.get("/")[2] and 'href="/agents"' in viewer.get("/agents")[2]


def test_hostile_folder_and_agent_text_is_shown_as_text(viewer, monkeypatch):
    monkeypatch.setattr(server_module, "scan_agents", hostile_agent)
    status, _, body = viewer.get("/agents")
    assert status == 200 and "&lt;script&gt;" in body
    assert "<script" not in body.lower() and "<img" not in body.lower() and XSS not in body
    assert not re.search(r"<[^>]*\son\w+\s*=", body.lower())


def test_the_only_command_shown_is_fixed_text_never_built_from_a_name(viewer, monkeypatch):
    monkeypatch.setattr(server_module, "scan_agents", hostile_agent)
    pre = re.findall(r"<pre>(.*?)</pre>", viewer.get("/agents")[2], re.DOTALL)
    assert pre == ["memdebug setup"]


def test_the_agents_page_needs_no_ledger(viewer, monkeypatch):
    def broken(*args, **kwargs):
        raise LedgerError("cannot open")
    monkeypatch.setattr(server_module.Ledger, "open_readonly", broken)
    assert viewer.get("/")[0] == 503 and viewer.get("/agents")[0] == 200


def test_the_agents_page_answers_get_only(viewer):
    assert viewer.request("POST", "/agents")[0] == 405 and viewer.request("PUT", "/agents")[0] == 405
    assert viewer.get("/agents", cookie=False)[0] == 403  # still behind the secret

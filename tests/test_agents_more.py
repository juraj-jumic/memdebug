"""The catalog entries added for 1.0 (each from the agent's own documentation, see docs/agents.md), the environment variables that move
an agent's folder, and the .txt option for Goose. Every home folder here is made up."""
import os

import pytest
from typer.testing import CliRunner

import memdebug.agents as ag
import memdebug.cli as cli
import memdebug.stores as st
from memdebug.adapters.folder import FolderAdapter
from memdebug.errors import AdapterError

runner = CliRunner()


def touch(path, text="x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def candidates(home, **kw):
    return {f.agent.key: {c.name: c for c in f.candidates} for f in ag.scan_agents(home, **kw)}


# -- what each added agent offers ---------------------------------------------------------------------------------------------

def test_claude_code_also_offers_its_rules_folder_and_every_subagents_memory(tmp_path):
    home = tmp_path / "home"
    touch(home / ".claude" / "rules" / "style.md")
    touch(home / ".claude" / "agent-memory" / "reviewer" / "MEMORY.md")
    touch(home / ".claude" / "agent-memory" / "planner" / "notes.md")
    touch(home / ".claude" / "agent-memory" / "empty" / "readme.txt")  # no markdown: not offered
    got = candidates(home)["claude-code"]
    assert got["claude-code-rules"].path == home / ".claude" / "rules" and got["claude-code-rules"].files is None
    sub = {c.path.name for n, c in got.items() if n.startswith("claude-agent-memory-")}
    assert sub == {"reviewer", "planner"} and all(st.valid_name(n) for n in got)


def test_gemini_cli_offers_its_private_per_project_memory_notes_beside_gemini_md(tmp_path):
    home = tmp_path / "home"
    touch(home / ".gemini" / "GEMINI.md")
    touch(home / ".gemini" / "tmp" / "my-project" / "memory" / "MEMORY.md")
    touch(home / ".gemini" / "tmp" / "other" / "chats" / "session.md")  # chat history is not memory: only .../memory is offered
    got = candidates(home)["gemini-cli"]
    assert got["gemini-cli"].files == ("GEMINI.md",)
    assert [c.path for n, c in got.items() if n != "gemini-cli"] == [home / ".gemini" / "tmp" / "my-project" / "memory"]


def test_codex_offers_its_generated_memories_only_when_they_exist(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    assert candidates(home)["codex-cli"] == {}
    touch(home / ".codex" / "memories" / "MEMORY.md")
    got = candidates(home)["codex-cli"]
    assert list(got) == ["codex-memories"] and got["codex-memories"].path == home / ".codex" / "memories"


def test_the_other_added_agents_are_found_with_exactly_what_they_keep(tmp_path):
    home = tmp_path / "home"
    touch(home / ".hermes" / "memories" / "MEMORY.md")
    touch(home / ".hermes" / "memories" / "notes.md")      # not one of its two files: never listed
    touch(home / ".roo" / "rules" / "a.md")
    touch(home / ".cline" / "rules" / "b.md")
    touch(home / ".copilot" / "copilot-instructions.md")
    touch(home / ".copilot" / "instructions" / "x.instructions.md")
    touch(home / ".copilot" / "config.json")                # beside the instructions: never offered
    touch(home / "basic-memory" / "ideas.md")
    got = candidates(home)
    assert got["hermes"]["hermes"].files == ("MEMORY.md", "USER.md")
    assert got["roo-code"]["roo-code"].path == home / ".roo" / "rules"
    assert got["cline"]["cline-home"].path == home / ".cline" / "rules"
    assert got["copilot-cli"]["copilot-cli"].files == ("copilot-instructions.md",) and got["copilot-cli"]["copilot-instructions"].files is None
    assert got["basic-memory"]["basic-memory"].path == home / "basic-memory"


@pytest.mark.parametrize("where", [(".config", "zed"), ("AppData", "Roaming", "Zed")], ids=["posix", "windows"])
def test_zed_is_found_in_either_system_form(tmp_path, where):
    home = tmp_path / "home"
    touch(home.joinpath(*where, "AGENTS.md"))
    [cand] = candidates(home)["zed"].values()
    assert cand.path == home.joinpath(*where) and cand.files == ("AGENTS.md",)


@pytest.mark.parametrize("where", [(".config", "goose", "memory"), ("AppData", "Roaming", "Block", "goose", "config", "memory")], ids=["posix", "windows"])
def test_goose_memory_is_offered_as_text_files(tmp_path, where):
    home = tmp_path / "home"
    touch(home.joinpath(*where, "personal.txt"))
    touch(home.joinpath(*where, "ignored.md"))
    [cand] = candidates(home)["goose"].values()
    assert cand.path == home.joinpath(*where) and cand.suffix == ".txt"


def test_no_agent_is_found_on_a_computer_that_has_none_of_them(tmp_path):
    assert ag.scan_agents(tmp_path / "empty") == []


# -- environment variables that move an agent's folder --------------------------------------------------------------------------

def test_claude_config_dir_moves_everything_under_claude(tmp_path):
    home, moved = tmp_path / "home", tmp_path / "elsewhere" / "claude-config"
    touch(moved / "CLAUDE.md")
    touch(moved / "projects" / "-p" / "memory" / "MEMORY.md")
    touch(home / ".claude" / "CLAUDE.md")   # the usual place is ignored while the variable is set
    got = candidates(home, env={"CLAUDE_CONFIG_DIR": str(moved)})["claude-code"]
    assert got["claude-code-global"].path == moved and got["claude-p"].path == moved / "projects" / "-p" / "memory"
    assert candidates(home)["claude-code"]["claude-code-global"].path == home / ".claude"  # without the variable


def test_the_workspace_variable_beats_the_state_variable_for_openclaw(tmp_path):
    home, state, workspace = tmp_path / "home", tmp_path / "state", tmp_path / "ws"
    (state / "workspace").mkdir(parents=True)
    workspace.mkdir()
    touch(workspace / "MEMORY.md")
    both = candidates(home, env={"OPENCLAW_STATE_DIR": str(state), "OPENCLAW_WORKSPACE_DIR": str(workspace)})
    assert both["openclaw"]["openclaw"].path == workspace
    only_state = candidates(home, env={"OPENCLAW_STATE_DIR": str(state)})
    assert only_state["openclaw"]["openclaw"].path == state / "workspace"


@pytest.mark.parametrize("value", ["relative/path", "", "ESCAPE", "LONG", "NEWLINE"], ids=["relative", "empty", "escape", "long", "newline"])
def test_an_unusable_variable_is_ignored(tmp_path, value):
    home = tmp_path / "home"
    touch(home / ".claude" / "CLAUDE.md")
    # built from tmp_path so each is an absolute path on every system and is refused for its own fault only
    value = {"ESCAPE": str(tmp_path / "x\x1b[2J"), "LONG": str(tmp_path / ("a" * 500)), "NEWLINE": str(tmp_path / "ok\nx")}.get(value, value)
    assert candidates(home, env={"CLAUDE_CONFIG_DIR": value})["claude-code"]["claude-code-global"].path == home / ".claude"


def test_a_variable_that_moves_only_the_workspace_does_not_by_itself_make_openclaw_look_installed(tmp_path):
    home, workspace = tmp_path / "home", tmp_path / "ws"
    touch(workspace / "MEMORY.md")
    assert "openclaw" not in candidates(home, env={"OPENCLAW_WORKSPACE_DIR": str(workspace)})  # only ~/.openclaw says it is installed
    (home / ".openclaw").mkdir(parents=True)
    assert candidates(home, env={"OPENCLAW_WORKSPACE_DIR": str(workspace)})["openclaw"]["openclaw"].path == workspace


def test_a_place_with_named_files_and_text_notes_keeps_both_settings(tmp_path, monkeypatch):
    agent = ag.Agent("demo", "Demo", (".demo",), "a made-up agent", (ag.Place((".demo",), ("a.txt",), "demo", "its a.txt", suffix=".txt"),))
    monkeypatch.setattr(ag, "AGENTS", (agent,))
    home = tmp_path / "home"
    touch(home / ".demo" / "a.txt")
    [cand] = ag.scan_agents(home)[0].candidates
    assert cand.files == ("a.txt",) and cand.suffix == ".txt"


def test_the_real_environment_is_not_read_when_a_home_folder_is_given(tmp_path, monkeypatch):
    home, other = tmp_path / "home", tmp_path / "other"
    touch(home / ".claude" / "CLAUDE.md")
    touch(other / "CLAUDE.md")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    assert candidates(home)["claude-code"]["claude-code-global"].path == home / ".claude"


def test_the_real_environment_is_read_for_a_real_run(tmp_path, monkeypatch):
    other = tmp_path / "other"
    touch(other / "CLAUDE.md")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "nohome"))
    found = {f.agent.key: f for f in ag.scan_agents()}
    assert found["claude-code"].candidates[0].path == other


# -- the catalog itself ---------------------------------------------------------------------------------------------------------

def test_every_entry_is_well_formed():
    keys = [a.key for a in ag.AGENTS]
    assert len(keys) == len(set(keys))
    for agent in ag.AGENTS:
        for place in agent.places:
            assert st.valid_name(place.name), place.name
            assert all(isinstance(p, str) and p and ".." not in p and "/" not in p for p in place.parts)
            assert place.suffix in st.SUFFIXES and all(f.lower().endswith(place.suffix) for f in place.files)
        for group in agent.groups:
            assert st.valid_name(group.prefix.rstrip("-") or "x") and all(p and ".." not in p for p in group.base + group.inside)
        for variable, prefix in agent.relocate:
            assert variable.isupper() and variable.replace("_", "").isalnum() and prefix[:len(agent.sign)] == agent.sign
    names = [p.name for a in ag.AGENTS for p in a.places]
    duplicated = {n for n in names if names.count(n) > 1}
    assert duplicated == {"zed", "goose"}  # the same place in its two system forms: only one can exist on a computer


# -- watching .txt files (Goose) ------------------------------------------------------------------------------------------------

def test_the_folder_reader_can_watch_text_files_and_leaves_markdown_alone(tmp_path):
    folder = tmp_path / "memory"
    touch(folder / "personal.txt", "likes tea\n\nhas a dog\n")
    touch(folder / "notes.md", "not watched\n")
    live = FolderAdapter(folder, store="g", suffixes=(".txt",)).list_memories({"store": "g"})
    assert [m.id for m in live.memories] == ["personal.txt"] and live.complete


def test_the_registry_accepts_a_suffix_only_for_folders_and_only_md_or_txt():
    ok = st.validate_store({"name": "goose", "kind": "folder", "path": "/x", "suffix": ".txt"})
    assert ok.suffix == ".txt"
    for bad in (".exe", "txt", ".MD", ".md/x", ".txt,.md"):
        with pytest.raises(st.SettingsError):
            st.validate_store({"name": "g", "kind": "folder", "path": "/x", "suffix": bad})
    with pytest.raises(st.SettingsError):
        st.validate_store({"name": "g", "kind": "openwebui", "path": "/x", "suffix": ".txt"})


def test_files_are_checked_against_the_stores_own_suffix():
    assert st.validate_store({"name": "g", "kind": "folder", "path": "/x", "suffix": ".txt", "files": "a.txt,b.txt"}).files == "a.txt,b.txt"
    with pytest.raises(st.SettingsError):
        st.validate_store({"name": "g", "kind": "folder", "path": "/x", "suffix": ".txt", "files": "a.md"})
    with pytest.raises(st.SettingsError):
        st.validate_store({"name": "g", "kind": "folder", "path": "/x", "files": "a.txt"})


def test_add_with_a_suffix_watches_the_text_files_and_check_sees_a_change(tmp_path):
    folder = tmp_path / "memory"
    touch(folder / "personal.txt", "likes tea\n")
    touch(folder / "other.md", "never watched\n")
    db = tmp_path / "l.db"
    added = runner.invoke(cli.app, ["add", str(folder), "--suffix", ".txt", "--name", "goose", "--db", str(db)])
    assert added.exit_code == 0, added.output
    assert runner.invoke(cli.app, ["check", "--db", str(db), "--settle", "0"]).exit_code == 0
    (folder / "personal.txt").write_text("likes coffee\n", encoding="utf-8")
    (folder / "other.md").write_text("changed but not watched\n", encoding="utf-8")
    out = runner.invoke(cli.app, ["check", "--db", str(db), "--settle", "0"]).output
    assert "1 change noticed (edited: personal.txt" in out or "personal.txt" in out
    assert "other.md" not in out


def test_adding_with_a_bad_suffix_is_refused_clearly(tmp_path):
    folder = tmp_path / "memory"
    touch(folder / "a.md")
    out = runner.invoke(cli.app, ["add", str(folder), "--suffix", ".exe", "--db", str(tmp_path / "l.db")])
    assert out.exit_code != 0 and "suffix" in out.output.lower()


def test_setup_offers_goose_with_its_suffix_and_registers_it(tmp_path, monkeypatch):
    home = tmp_path / "home"
    touch(home / ".config" / "goose" / "memory" / "personal.txt", "likes tea\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PATH", str(home))  # no docker
    for variable in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "COPILOT_HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_WORKSPACE_DIR"):
        monkeypatch.delenv(variable, raising=False)
    db = tmp_path / "l.db"
    out = runner.invoke(cli.app, ["setup", "--yes", "--db", str(db)])
    assert out.exit_code == 0, out.output
    store = st.load_registry(db.with_name("stores.json")).get("goose")
    assert store is not None and store.suffix == ".txt" and store.kind == "folder"


def test_a_registry_without_a_suffix_still_loads_and_reads_markdown(tmp_path):
    folder = tmp_path / "n"
    touch(folder / "a.md", "hello\n")
    cfg = st.validate_store({"name": "n", "kind": "folder", "path": str(folder)})
    assert cfg.suffix is None
    assert [m.id for m in st.open_store(cfg).adapter.list_memories({"store": "n"}).memories] == ["a.md"]


def test_the_adapter_still_refuses_a_suffix_that_is_not_lowercase_and_dotted(tmp_path):
    with pytest.raises(AdapterError):
        FolderAdapter(tmp_path, store="s", suffixes=("TXT",))


def test_nothing_in_this_file_depends_on_the_real_home_folder():
    assert not os.environ.get("MEMDEBUG_TEST_USES_REAL_HOME")

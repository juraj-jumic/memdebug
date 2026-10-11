"""Which agents are on this computer, and where each keeps what it remembers.

Each entry below was checked against the agent's own documentation (see docs/agents.md for the sources and how sure each is). Presence is
judged only by whether a known folder exists (`lstat`; nothing is opened or read, links and junctions are ignored). Where an agent keeps
memory in one file inside a folder that also holds settings or credentials (`~/.gemini`, `~/.codex`, `~/.claude`), only that file is offered,
never the folder.

A few agents document an environment variable that moves their folder (for example `CLAUDE_CONFIG_DIR`). It is honoured for a real run, when it
holds an absolute, plain path; the same variable is never read when a home folder is given (tests).

Assistants that keep their memory in the provider's cloud (ChatGPT, Claude's apps, Gemini, Copilot) have nothing on this computer
to watch, and memdebug says so instead of pretending: it cannot tell whether those apps are installed, and it could not read
their memory if they were.
"""
from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import longpath
from .adapters.markdown_git import _is_reparse_point
from .stores import Candidate, discover_children
from .textsafe import has_unsafe_chars

MAX_ENV_PATH = 400


@dataclass(frozen=True)
class Place:
    """A folder under the home folder where an agent keeps what it remembers, and how to offer it for watching.

    Attributes:
        parts: The folder, as path parts relative to the home folder.
        files: If set, only these files in the folder are offered, nothing else.
        name: The suggested store name.
        what: A short description of the contents, shown to the person.
        always: Whether to offer the place even before any note exists in it, because the agent creates the folder itself.
        suffix: The file ending of the notes when it is not ".md" (Goose keeps ".txt" files).
    """

    parts: tuple[str, ...]      # folder, relative to the home folder
    files: tuple[str, ...] = ()  # if set: only these files in that folder, nothing else
    name: str = ""              # suggested store name
    what: str = ""
    always: bool = False        # offer even before any note exists (the agent creates the folder itself)
    suffix: str = ".md"


@dataclass(frozen=True)
class Group:
    """Memory folders kept one per project or per agent: each child of `base` that holds notes at `inside`.

    Attributes:
        base: The folder whose children are the per-project folders, relative to the home folder.
        inside: The path inside each child where the notes are (empty if the child itself holds them).
        prefix: What the suggested store names start with.
        what: The description shown to the person.
    """

    base: tuple[str, ...]
    inside: tuple[str, ...]
    prefix: str
    what: str


@dataclass(frozen=True)
class Agent:
    """A known AI agent: how to tell it is installed and where its memory lives.

    Attributes:
        key: A short identifier for the agent, such as "claude-code".
        name: The agent's display name.
        sign: A folder under home (as path parts) whose existence means the agent is probably installed.
        note: A one-line description of how the agent keeps its memory, shown to the person.
        places: The places where the agent's memory can be watched.
        groups: Per-project or per-agent memory folders to look for.
        alt_signs: Other folders whose existence also means the agent is installed (a different place on another system).
        relocate: Environment variables that move part of the agent's folder, as (variable, the folder it replaces) pairs.
    """

    key: str
    name: str
    sign: tuple[str, ...]       # a folder under home whose existence means the agent is probably installed
    note: str
    places: tuple[Place, ...] = ()
    groups: tuple[Group, ...] = ()
    alt_signs: tuple[tuple[str, ...], ...] = ()
    relocate: tuple[tuple[str, tuple[str, ...]], ...] = ()


AGENTS: tuple[Agent, ...] = (
    Agent("claude-code", "Claude Code", (".claude",),
          "keeps notes in CLAUDE.md files, a rules folder, and a memory folder per project and per subagent",
          (Place((".claude",), ("CLAUDE.md",), "claude-code-global", "your global CLAUDE.md instructions"),
           Place((".claude", "rules"), (), "claude-code-rules", "your global Claude Code rules (the markdown files in it)")),
          groups=(Group((".claude", "projects"), ("memory",), "claude-", "Claude Code's memory notes for one project"),
                  Group((".claude", "agent-memory"), (), "claude-agent-memory-", "Claude Code subagent memory (one agent's notes)")),
          relocate=(("CLAUDE_CONFIG_DIR", (".claude",)),)),
    Agent("openclaw", "OpenClaw", (".openclaw",),
          "keeps its memory as markdown in a workspace folder (MEMORY.md, memory/, USER.md); its documentation suggests keeping it in git",
          (Place((".openclaw", "workspace"), (), "openclaw", "the OpenClaw workspace (memory, identity and instruction files)", always=True),),
          relocate=(("OPENCLAW_STATE_DIR", (".openclaw",)), ("OPENCLAW_WORKSPACE_DIR", (".openclaw", "workspace")))),
    Agent("gemini-cli", "Gemini CLI", (".gemini",),
          "follows ~/.gemini/GEMINI.md and keeps private memory notes per project under ~/.gemini/tmp",
          (Place((".gemini",), ("GEMINI.md",), "gemini-cli", "GEMINI.md, its global instructions and memory file", always=True),),
          groups=(Group((".gemini", "tmp"), ("memory",), "gemini-", "Gemini CLI's private memory notes for one project"),)),
    Agent("codex-cli", "Codex CLI", (".codex",),
          "follows the instructions in ~/.codex/AGENTS.md (so planted text there matters) and, only if you switch the feature on, "
          "writes generated memories to ~/.codex/memories",
          (Place((".codex",), ("AGENTS.md", "AGENTS.override.md"), "codex-cli", "its AGENTS.md instruction files"),
           Place((".codex", "memories"), (), "codex-memories", "its generated memories (the markdown files in it)")),
          relocate=(("CODEX_HOME", (".codex",)),)),
    Agent("windsurf", "Windsurf", (".codeium", "windsurf"),
          "stores its automatically generated memories on this computer only",
          (Place((".codeium", "windsurf", "memories"), (), "windsurf", "Cascade's memories (the markdown files in it)"),)),
    Agent("cline", "Cline", ("Documents", "Cline"),
          "has no memory store of its own; it follows the rules in your global Cline Rules folder, which is why planted text there matters",
          (Place(("Documents", "Cline", "Rules"), (), "cline", "your global Cline Rules (the markdown files in it)"),
           Place((".cline", "rules"), (), "cline-home", "rules in ~/.cline/rules (the markdown files in it)")),
          alt_signs=((".cline",),)),
    Agent("roo-code", "Roo Code", (".roo",),
          "has no memory store of its own; it follows the rules in ~/.roo/rules, which is why planted text there matters",
          (Place((".roo", "rules"), (), "roo-code", "your global Roo Code rules (the markdown files in it)"),)),
    Agent("hermes", "Hermes Agent", (".hermes",),
          "keeps what it learns about you in two files, MEMORY.md and USER.md",
          (Place((".hermes", "memories"), ("MEMORY.md", "USER.md"), "hermes", "its MEMORY.md and USER.md", always=True),)),
    Agent("goose", "Goose", (".config", "goose"),
          "keeps what it is asked to remember as text files, one per category",
          (Place((".config", "goose", "memory"), (), "goose", "its saved memories (the .txt files in it)", suffix=".txt"),
           Place(("AppData", "Roaming", "Block", "goose", "config", "memory"), (), "goose", "its saved memories (the .txt files in it)", suffix=".txt")),
          alt_signs=(("AppData", "Roaming", "Block", "goose"),)),
    Agent("copilot-cli", "GitHub Copilot CLI", (".copilot",),
          "follows your personal instruction files in ~/.copilot; its Copilot Memory feature lives in GitHub's cloud and cannot be seen here",
          (Place((".copilot",), ("copilot-instructions.md",), "copilot-cli", "your personal copilot-instructions.md"),
           Place((".copilot", "instructions"), (), "copilot-instructions", "your personal instruction files (the markdown files in it)")),
          relocate=(("COPILOT_HOME", (".copilot",)),)),
    Agent("zed", "Zed", (".config", "zed"),
          "follows a global AGENTS.md; rules saved in its Rules Library are in a database that memdebug cannot read",
          (Place((".config", "zed"), ("AGENTS.md",), "zed", "your global AGENTS.md"),
           Place(("AppData", "Roaming", "Zed"), ("AGENTS.md",), "zed", "your global AGENTS.md")),
          alt_signs=(("AppData", "Roaming", "Zed"),)),
    Agent("basic-memory", "Basic Memory", ("basic-memory",),
          "keeps notes as markdown files in ~/basic-memory (an MCP memory server; your notes folder may be somewhere else: use 'memdebug add')",
          (Place(("basic-memory",), (), "basic-memory", "your Basic Memory notes (the markdown files in it)"),)),
)

CLOUD_NOTE = ("ChatGPT, Claude (claude.ai and its desktop and phone apps), Gemini and Copilot keep their memory in the provider's cloud. "
              "memdebug cannot see it, and cannot tell whether those apps are installed. Review or clear it in each app's settings; "
              "to keep a record of how it changes, copy it into a markdown file now and then, in a folder you add to memdebug.")


@dataclass
class FoundAgent:
    """A known agent that appears to be installed, with the places memdebug could watch for it.

    Attributes:
        agent: The catalog entry.
        candidates: The watchable places found on this computer. Empty when there is nothing to watch yet.
    """

    agent: Agent
    candidates: list[Candidate] = field(default_factory=list)


def _real_folder(path: Path) -> bool:
    try:
        info = longpath.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not _is_reparse_point(info)


def _has_notes(path: Path, suffix: str) -> bool:
    try:
        return any(name.lower().endswith(suffix) for name in longpath.listdir(path)[:2000])
    except OSError:
        return False


def _is_plain_file(path: Path) -> bool:
    try:
        info = longpath.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not _is_reparse_point(info)


def _usable_env_path(value: object) -> bool:
    """Whether an environment variable holds a plain absolute path worth following: not empty, not huge, no control characters."""
    return isinstance(value, str) and 0 < len(value) <= MAX_ENV_PATH and not has_unsafe_chars(value) and Path(value).is_absolute()


def _locate(root: Path, variables: Mapping[str, str], agent: Agent, parts: tuple[str, ...]) -> Path:
    """Where `parts` (relative to the home folder) is for this agent, honouring the longest environment variable that moves it."""
    best: tuple[str, tuple[str, ...]] | None = None
    for variable, prefix in agent.relocate:
        value = variables.get(variable)
        if value and parts[:len(prefix)] == prefix and _usable_env_path(value) and (best is None or len(prefix) > len(best[1])):
            best = (value, prefix)
    if best is not None:
        return Path(best[0]).joinpath(*parts[len(best[1]):])
    return root.joinpath(*parts)


def scan_agents(home: Path | None = None, env: Mapping[str, str] | None = None) -> list[FoundAgent]:
    """The known agents that appear to be installed, with what could be watched for each. Reads no file contents.

    Args:
        home: The home folder to look in; the real one when omitted.
        env: The environment variables that can move an agent's folder. The real environment for a real run (no `home`); none when a
            `home` is given, unless this is passed too.
    """
    root = home or Path.home()
    variables: Mapping[str, str] = os.environ if (home is None and env is None) else (env or {})
    found: list[FoundAgent] = []
    for agent in AGENTS:
        def where(parts: tuple[str, ...], agent: Agent = agent) -> Path:
            return _locate(root, variables, agent, parts)

        if not any(_real_folder(where(sign)) for sign in (agent.sign, *agent.alt_signs)):
            continue
        entry = FoundAgent(agent)
        for place in agent.places:
            folder = where(place.parts)
            if not _real_folder(folder):
                continue
            suffix = None if place.suffix == ".md" else place.suffix
            if place.files:
                present = [name for name in place.files if _is_plain_file(folder / name)]
                if not present and not place.always:
                    continue
                entry.candidates.append(Candidate("folder", place.name, folder, f"{agent.name}: {place.what}", files=place.files, suffix=suffix))
            elif place.always or _has_notes(folder, place.suffix):
                entry.candidates.append(Candidate("folder", place.name, folder, f"{agent.name}: {place.what}", suffix=suffix))
        for group in agent.groups:  # per-project memory folders are found by name
            entry.candidates += [Candidate(c.kind, c.name, c.path, f"{agent.name}: {c.why}") for c in
                                 discover_children(where(group.base), group.inside, prefix=group.prefix, why=group.what)]
        found.append(entry)
    return found


def summary_lines(found: list[FoundAgent], docker_names: list[str] | None = None) -> list[str]:
    """Lines of text describing the found agents, for the person to read.

    Args:
        found: The result of `scan_agents`.
        docker_names: Names of running Open WebUI containers, each listed as one more place to watch.

    Returns:
        One line per agent and per container (a single line saying none were found if there are neither), then a blank line
        and the note about assistants whose memory is in the cloud.
    """
    lines = []
    for item in found:
        watchable = f"{len(item.candidates)} place(s) memdebug can watch" if item.candidates else "nothing to watch yet"
        lines.append(f"  {item.agent.name}: {item.agent.note} ({watchable})")
    for container in docker_names or []:
        lines.append(f"  Open WebUI (running in Docker as '{container}'): keeps its memory in a database (1 place memdebug can watch)")
    if not lines:
        lines.append("  None of the agents memdebug knows were found.")
    lines.append("")
    lines.append(f"  {CLOUD_NOTE}")
    return lines

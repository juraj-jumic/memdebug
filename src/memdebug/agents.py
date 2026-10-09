"""Which agents are on this computer, and where each keeps what it remembers.

Each entry below was checked against the agent's own documentation. Presence is judged only by whether a known folder exists
(`lstat`; nothing is opened or read, links and junctions are ignored). Where an agent keeps memory in one file inside a folder
that also holds settings or credentials (`~/.gemini`, `~/.codex`, `~/.claude`), only that file is offered, never the folder.

Assistants that keep their memory in the provider's cloud (ChatGPT, Claude's apps, Gemini, Copilot) have nothing on this computer
to watch, and memdebug says so instead of pretending: it cannot tell whether those apps are installed, and it could not read
their memory if they were.
"""
from __future__ import annotations

import stat
from dataclasses import dataclass, field
from pathlib import Path

from . import longpath
from .adapters.markdown_git import _is_reparse_point
from .stores import Candidate, discover


@dataclass(frozen=True)
class Place:
    """A folder under the home folder where an agent keeps what it remembers, and how to offer it for watching.

    Attributes:
        parts: The folder, as path parts relative to the home folder.
        files: If set, only these files in the folder are offered, nothing else.
        name: The suggested store name.
        what: A short description of the contents, shown to the person.
        always: Whether to offer the place even before any note exists in it, because the agent creates the folder itself.
    """

    parts: tuple[str, ...]      # folder, relative to the home folder
    files: tuple[str, ...] = ()  # if set: only these files in that folder, nothing else
    name: str = ""              # suggested store name
    what: str = ""
    always: bool = False        # offer even before any note exists (the agent creates the folder itself)


@dataclass(frozen=True)
class Agent:
    """A known AI agent: how to tell it is installed and where its memory lives.

    Attributes:
        key: A short identifier for the agent, such as "claude-code".
        name: The agent's display name.
        sign: A folder under home (as path parts) whose existence means the agent is probably installed.
        note: A one-line description of how the agent keeps its memory, shown to the person.
        places: The places where the agent's memory can be watched.
    """

    key: str
    name: str
    sign: tuple[str, ...]       # a folder under home whose existence means the agent is probably installed
    note: str
    places: tuple[Place, ...] = ()


AGENTS: tuple[Agent, ...] = (
    Agent("claude-code", "Claude Code", (".claude",),
          "keeps notes in CLAUDE.md files and a memory folder per project",
          (Place((".claude",), ("CLAUDE.md",), "claude-code-global", "your global CLAUDE.md instructions"),)),
    Agent("openclaw", "OpenClaw", (".openclaw",),
          "keeps its memory as markdown in a workspace folder (MEMORY.md, memory/, USER.md); its documentation suggests keeping it in git",
          (Place((".openclaw", "workspace"), (), "openclaw", "the OpenClaw workspace (memory, identity and instruction files)", always=True),)),
    Agent("gemini-cli", "Gemini CLI", (".gemini",),
          "saves what it is asked to remember in ~/.gemini/GEMINI.md",
          (Place((".gemini",), ("GEMINI.md",), "gemini-cli", "GEMINI.md, where its memory tool saves facts", always=True),)),
    Agent("codex-cli", "Codex CLI", (".codex",),
          "has no memory of its own; it follows the instructions in ~/.codex/AGENTS.md, which is why planted text there matters",
          (Place((".codex",), ("AGENTS.md", "AGENTS.override.md"), "codex-cli", "its AGENTS.md instruction files"),)),
    Agent("windsurf", "Windsurf", (".codeium", "windsurf"),
          "stores its automatically generated memories on this computer only",
          (Place((".codeium", "windsurf", "memories"), (), "windsurf", "Cascade's memories (the markdown files in it)"),)),
    Agent("cline", "Cline", ("Documents", "Cline"),
          "has no memory store of its own; it follows the rules in your global Cline Rules folder, which is why planted text there matters",
          (Place(("Documents", "Cline", "Rules"), (), "cline", "your global Cline Rules (the markdown files in it)"),)),
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


def _has_markdown(path: Path) -> bool:
    try:
        return any(name.lower().endswith(".md") for name in longpath.listdir(path)[:2000])
    except OSError:
        return False


def _is_plain_file(path: Path) -> bool:
    try:
        info = longpath.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not _is_reparse_point(info)


def scan_agents(home: Path | None = None) -> list[FoundAgent]:
    """The known agents that appear to be installed, with what could be watched for each. Reads no file contents."""
    root = home or Path.home()
    found: list[FoundAgent] = []
    for agent in AGENTS:
        if not _real_folder(root.joinpath(*agent.sign)):
            continue
        entry = FoundAgent(agent)
        for place in agent.places:
            folder = root.joinpath(*place.parts)
            if not _real_folder(folder):
                continue
            if place.files:
                present = [name for name in place.files if _is_plain_file(folder / name)]
                if not present and not place.always:
                    continue
                entry.candidates.append(Candidate("folder", place.name, folder, f"{agent.name}: {place.what}", files=place.files))
            elif place.always or _has_markdown(folder):
                entry.candidates.append(Candidate("folder", place.name, folder, f"{agent.name}: {place.what}"))
        if agent.key == "claude-code":  # its per-project memory folders are found by name, as before
            entry.candidates += [Candidate(c.kind, c.name, c.path, f"{agent.name}: {c.why}") for c in discover(root)]
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

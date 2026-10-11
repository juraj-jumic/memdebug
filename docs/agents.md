# Agents memdebug knows about

`memdebug agents` lists which of these appear to be on this computer; `memdebug setup` offers to watch what it finds. Presence is
judged only by whether a known folder exists. Nothing is opened or read, and links and junctions are ignored.

Where an agent keeps memory in a single file inside a folder that also holds settings or credentials, memdebug watches that file
only. It does not list or open anything else in the folder.

## How sure each entry is

Only two entries have been tried on a real installation by the maintainer: **Claude Code** (Windows 11) and **Open WebUI** (Docker, versions 0.11.4 and
0.12.0). Every other entry follows the agent's own documentation or source code as read on 2026-10-11, which can be wrong or out of date: these
tools change their folders between versions. Each row says which kind of source it rests on:

* **tried**: seen working on a real installation.
* **docs**: the vendor's official documentation.
* **source**: read in the project's source code.
* **third party**: only blog posts or forum threads (listed only where nothing better exists).

If a location is wrong for you, `memdebug add <path>` watches any folder (see [usage.md](usage.md)), and please tell us.

## The catalog

| Agent | Looks for | Watches | Source and confidence |
| --- | --- | --- | --- |
| Claude Code | `~/.claude` (or `$CLAUDE_CONFIG_DIR`) | the single file `CLAUDE.md`; the markdown files in `rules/`; each project's memory folder `projects/*/memory`; each subagent's folder `agent-memory/*` | **tried** on Windows 11: `.claude\projects\<project>\memory\` holds `MEMORY.md` (an index) and one `.md` file per note, and the global `CLAUDE.md`. `rules/` and `agent-memory/` are **docs** only (code.claude.com/docs/en/memory, /sub-agents): they did not exist on the tried installation. memdebug lists the project folders that exist, it does not work out their names (the documentation describes the naming two ways). A memory directory moved with the `autoMemoryDirectory` setting, and the files that live inside a repository (`CLAUDE.md`, `.claude/rules`, `.claude/agent-memory`), are not under the home folder: add them with `memdebug add`. Not checked on macOS or Linux |
| OpenClaw | `~/.openclaw` (or `$OPENCLAW_STATE_DIR`) | the workspace `workspace/` (or `$OPENCLAW_WORKSPACE_DIR`): `MEMORY.md`, `USER.md`, `DREAMS.md`, `memory/` and the instruction files | **docs** (docs.openclaw.ai, the agent workspace and memory pages). Other profiles (`OPENCLAW_PROFILE`) and per-agent workspaces live in other folders: use `memdebug add`. Its search index is a SQLite file elsewhere and is not read |
| Gemini CLI | `~/.gemini` | the single file `GEMINI.md`; each project's private memory notes `tmp/*/memory` | **docs** and **source** (geminicli.com/docs/tools/memory, `storage.ts` in the repository). The older `save_memory` tool that appended a "Gemini Added Memories" section no longer exists in recent versions; the model now edits these files like any other. The Antigravity CLI that replaced it for some users is **not** covered (its paths are unverified) |
| Codex CLI | `~/.codex` (or `$CODEX_HOME`) | the single files `AGENTS.md` and `AGENTS.override.md`; the markdown files in `memories/` | **docs** and **source** (the Codex documentation and `codex-rs/memories`). `memories/` exists only if you switch the memory feature on; it is generated state and has a git repository of its own inside |
| Windsurf | `~/.codeium/windsurf` | the markdown files in `memories/` (Cascade's memories and `global_rules.md`) | **docs**. The vendor documents the folder but not the file format, so the `.md` filter may miss files; the product may now be called Devin Desktop and keep rules in `.devin/rules`, which are in repositories and are not covered. Not tried |
| Cline | `~/Documents/Cline` or `~/.cline` | the markdown files in `Documents/Cline/Rules` and in `.cline/rules` | **docs** (docs.cline.bot, Cline Rules). Cline has no memory store of its own; the "Memory Bank" is a convention of `memory-bank/*.md` files inside a project, added with `memdebug add`. If Windows redirects Documents (for example to OneDrive) the folder is not found: use `memdebug add`. Not tried |
| Roo Code | `~/.roo` | the markdown files in `rules/` | **docs** (Roo Code custom instructions). Mode-specific `rules-<mode>/` folders are not covered. Not tried |
| Hermes Agent | `~/.hermes` | the two files `memories/MEMORY.md` and `memories/USER.md` | **docs** (Hermes Agent's memory page). Not tried |
| Goose | `~/.config/goose` (Windows: `%APPDATA%\Block\goose`) | the `.txt` files in `memory/`, one per category | **source** (`crates/goose-mcp/src/memory`) and docs for the folder. Entries are separated by blank lines; its macOS folder is uncertain. Not tried |
| GitHub Copilot CLI | `~/.copilot` (or `$COPILOT_HOME`) | the single file `copilot-instructions.md`; the markdown files in `instructions/` | **docs** (docs.github.com, custom instructions for Copilot CLI). Copilot Memory itself lives in GitHub's cloud. VS Code's separate local memory tool has no documented folder and is not covered. Not tried |
| Zed | `~/.config/zed` (Windows: `%APPDATA%\Zed`) | the single file `AGENTS.md` | **docs** (Zed's rules documentation). Rules saved in its Rules Library are in a binary database and cannot be read. Not tried |
| Basic Memory | `~/basic-memory` | the markdown files in it | **docs** (the Basic Memory README). The notes folder is a setting, so yours may be elsewhere. Not tried |
| MCP memory server | by path, `memdebug add <path>/memory.jsonl` | the file `memory.jsonl` of the official `modelcontextprotocol/servers` memory server: each entity and each relation is one memory | **source** (`src/memory/index.ts`). Where the file is depends on how it was installed (the `MEMORY_FILE_PATH` setting, or the server's own folder), so memdebug cannot look for it. The server rewrites the whole file on every change by renaming a temporary file over it, which the reader copes with. Tested with made-up files only |
| Open WebUI | a running Docker container | the `memory` table of its database, copied read-only by memdebug; also its own `created_by` label (and, for tool writes, the model and the recorded chat and message ids) and the time and role (never the text) of chat messages, to say how close a chat was | **tried** (see below) |
| Mem0 (self-hosted) | by path (`history.db`) with `--user-id` | Mem0's own history and its memories, read through the `mem0ai` library | tested in CI against the real `mem0ai` 2.x library with a local store (its writer produces the history this reader follows); **not tried** on a long-running Mem0 server or the OpenMemory app, which keeps its data elsewhere |

### Open WebUI, in detail

* **Tried**: reading the real database in Docker (0.11.4 and 0.12.0), seeing a memory added, edited and removed in Settings, and one saved by a model
  through the native `add_memory` tool. A memory added by hand is labelled `created_by: manual`, `type: user`, and **so is a memory the model
  saved itself through `add_memory`**: the label cannot tell the two apart, and in a Temporary Chat (which stores no messages) the timing line says
  "consistent with being added by hand" as well. Memories written through the newer `update_memory` tool carry `created_by: tool` and the model and chat ids; those are shown as evidence, never as proof.
* **From the source, not tried**: `type` and `path` columns exist from 0.10 (older databases lack them and are read without them); `created_by` can also be `background_review`
  or any text a plugin writes, and is shown as a short plain label or only its size.
* **Not covered**: Postgres or SQLCipher installations (the copy from Docker then finds no SQLite file and says so, instead of reporting "no memories"), a change to
  only a memory's `type` or `path` (the text is unchanged, so no change is noticed), and the vector copies of memories that Open WebUI keeps for search.

## Not watchable from this computer

ChatGPT, Claude's apps (claude.ai on the web, desktop and phone), Gemini and Copilot keep their memory in the provider's cloud.
There is nothing on this computer to point memdebug at, and memdebug cannot tell whether those apps are installed, so it never
claims to have found them. Review or clear that memory in each app's settings. To keep a record of how it changes, copy it
into a markdown file now and then, in a folder you add to memdebug (`memdebug add <folder>`). A watched folder can be rolled back to a snapshot with `memdebug rollback store NAME --to s1`.

Not in the catalog on purpose: **Cursor** (its rules are `.cursor/rules/*.mdc` inside a repository, and where its Memories live is not documented), **Aider** (it has
no memory feature; its chat history is `.aider.chat.history.md` inside each project), **Continue** (rules are inside the workspace) and **Amp** (a personal `~/.config/amp/AGENTS.md`
is documented: `memdebug add ~/.config/amp --files AGENTS.md`). Files inside a repository are watched by adding that folder yourself.

## Adding an agent

An entry needs a documented location (link the documentation in the table above and say how sure it is), a test using a fake home folder (see
`tests/test_agents_more.py`), and a decision about whether to watch a folder or just the named files. Locations change between versions: if one is wrong, use
`memdebug add <path>` and tell us.

# Agents memdebug knows about

`memdebug agents` lists which of these appear to be on this computer; `memdebug setup` offers to watch what it finds. Presence is
judged only by whether a known folder exists. Nothing is opened or read, and links and junctions are ignored.

Where an agent keeps memory in a single file inside a folder that also holds settings or credentials, memdebug watches that file
only. It does not list or open anything else in the folder.

| Agent | Looks for | Watches | Source and confidence |
| --- | --- | --- | --- |
| Claude Code | `~/.claude` | each project's memory folder under `~/.claude/projects/*/memory`, and the single file `~/.claude/CLAUDE.md` | Anthropic's memory documentation for `CLAUDE.md`. The memory folder is confirmed on a real Windows 11 installation: `.claude\projects\<project>\memory\` holds `MEMORY.md` (an index) and one `.md` file per note. Not yet checked on macOS or Linux; Claude Code's `/memory` command shows the real location |
| OpenClaw | `~/.openclaw` | the workspace `~/.openclaw/workspace` (`MEMORY.md`, `memory/`, `USER.md`, instruction files) | OpenClaw's own documentation (agent workspace). If you moved the workspace, use `memdebug add <path>` |
| Gemini CLI | `~/.gemini` | the single file `~/.gemini/GEMINI.md`, where its `save_memory` tool appends facts | Gemini CLI's documentation (memory tool) |
| Codex CLI | `~/.codex` | the single files `~/.codex/AGENTS.md` and `AGENTS.override.md` | Third-party guides only. Codex has no memory feature; these are the instructions it follows, which is why planted text there matters |
| Windsurf | `~/.codeium/windsurf` | `~/.codeium/windsurf/memories` (the markdown files in it) | Windsurf's documentation (memories and rules). Auto-generated memories may not all be markdown; memdebug reads only `.md` files |
| Cline | `~/Documents/Cline` | `~/Documents/Cline/Rules` (the markdown files in it), the global rules Cline follows | Cline's documentation (Cline Rules, global directory: `Documents\Cline\Rules` on Windows, `~/Documents/Cline/Rules` on macOS and Linux). Not tried on a real installation. If Windows redirects Documents (for example to OneDrive) the folder is not found: use `memdebug add <path>`. Only `.md` files are read |
| Open WebUI | a running Docker container | the `memory` table of its database, copied read-only by memdebug; also its own `created_by` and `type` labels, and the time and role (never the text) of chat messages, to say how close a chat was | Confirmed on a real installation: a memory added by hand in Settings is labelled `created_by: manual`, `type: user`. Other label values are not confirmed |

## Not watchable from this computer

ChatGPT, Claude's apps (claude.ai on the web, desktop and phone), Gemini and Copilot keep their memory in the provider's cloud.
There is nothing on this computer to point memdebug at, and memdebug cannot tell whether those apps are installed, so it never
claims to have found them. Review or clear that memory in each app's settings. To keep a record of how it changes, copy it
into a markdown file now and then, in a folder you add to memdebug (`memdebug add <folder>`). A watched folder can be rolled back to a snapshot with `memdebug rollback store NAME --to s1`.

## Adding an agent

An entry needs a documented location (link the documentation in the table above), a test using a fake home folder, and a
decision about whether to watch a folder or just the named files. Locations change between versions: if one is wrong, use
`memdebug add <path>` and tell us.

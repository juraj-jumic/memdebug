# Open WebUI: a model that saves its own memories

This page records what was found on one real Open WebUI installation, and gives an example router for people who need the same fix. It is
not part of memdebug and nothing in CI runs it.

## What happened

A chat model that was really a *router* (an Open WebUI "Pipe" function that picks a local Ollama model and sends its own request) never saved a
memory. The model answered "saved" and even printed a made-up `<add_memory>` tag, but the memory table did not change. memdebug said "quiet",
which was right: nothing had been written.

The cause was in the router. Open WebUI gives a model its tools (memory, web search) in a `tools` list. The router rebuilt every request from
`role` and `content` only, so the model never saw a tool and could only pretend. The Settings page worked because it calls Open WebUI's memory
API directly, not the model.

With a plain model (`gpt-oss:20b`) and Function Calling set to **Native**, the model called `add_memory` itself and the memory appeared.

## What memdebug can and cannot say about it

`memdebug watch --every 5` reported every add, edit and removal within a few seconds in the test. A single `check` afterwards would have missed a memory
that was added and removed in between, because Open WebUI keeps no history.

On the write itself, Open WebUI labelled the memory the model saved `created_by: manual`, the same as one added by hand in Settings, and in a
Temporary Chat (which stores no messages) the timing line said "consistent with being added by hand". So the app's own label and the chat timing
cannot separate the assistant's writes from yours. `check` and `watch` print them as evidence, never as proof.

## The example router

[`docs/examples/open-webui-smart-router.py`](examples/open-webui-smart-router.py) is a router that forwards Open WebUI's tools. Compared with a
router that drops them it:

* passes the `tools` list to the chosen model and hands the model's tool calls back to Open WebUI (streaming and not), converting tool results
  back for Ollama;
* keeps the same model after a tool call instead of routing a second time;
* sends a request to a model that can call tools when web search is switched on or the message is about memory (`TOOL_ROUTES` says which routes
  those are; `!code`, `!think`, `!chat`, `!vision` and `!long` at the start of a message still win);
* if Ollama refuses tools for a model, answers without them and tells the model it has none;
* has a `LOADED_MODEL_BONUS` setting (off) to stay on the model already in memory.

**What was tried.** Open WebUI 0.12.0 in Docker, Ollama on Windows 11, native function calling, a Temporary Chat. `gpt-oss:20b` and `qwen3:14b`
called tools properly. `qwen2.5-coder:14b` wrote the call out as text instead of making it, and `gemma3:12b` has no tool support (it may still
say it saved something: do not use it for memory). Web search through a local SearXNG worked. A sample of 24 prompts went to the route the author
expected 24 times; the expectations are the author's own. **Not tried:** other Open WebUI versions, other models, Linux or macOS.

**To use it.** In Open WebUI: Admin Panel, Functions, Create, paste the file, save, switch it on. Then set Function Calling to Native for the
model (Admin Panel, Models) or, for one chat, in the chat's Controls. Functions run arbitrary code inside Open WebUI: read the file before you
enable it. Check that a memory really appears (Settings, Personalization) or run `memdebug watch` while you try.

It is provided as is. The example sentences in it are generic; change the model names and addresses at the top of the settings to match your
own Ollama.

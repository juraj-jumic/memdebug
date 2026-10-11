"""
title: Smart Router Tools
version: 2.3
description: Smart Router that also passes Open WebUI's tools (memory, search, ...) to the chosen Ollama model and hands its tool calls back. Needs Function Calling set to Native.
"""

import base64
import json
import math
import re
import uuid
from datetime import datetime
from typing import Awaitable, Callable, Optional

import aiohttp
from pydantic import BaseModel, Field

SCORED_ROUTES = ("code", "reasoning", "general")
LABELS = {
    "code": "Code",
    "reasoning": "Reasoning",
    "general": "General",
    "vision": "Vision",
    "long": "Long document",
}
OVERRIDES = {
    "code": "code",
    "think": "reasoning",
    "chat": "general",
    "vision": "vision",
    "long": "long",
}

# --------------------------------------------------------------------------
# System prompt. Edit freely; {date} is filled in automatically.
# --------------------------------------------------------------------------
BASE_PROMPT = """You are a helpful, knowledgeable assistant running locally on the user's own computer. Today is {date}.

Character and tone
- Be warm, direct and genuinely helpful. Treat the user as a capable adult and don't make negative assumptions about their judgment or abilities.
- Be honest even when it isn't what the user hopes to hear. Push back constructively and kindly when something is wrong, risky or based on a mistaken premise. Never flatter, and don't open with praise like "Great question".
- Use examples, analogies or thought experiments when they make an explanation clearer.
- Don't curse unless the user does, and then only sparingly.

Answering
- Match your effort to the request. A simple question gets a direct answer in a few sentences; a complex one gets a thorough, well-organised answer. When asked to change one thing in a longer text, change that thing instead of rewriting everything.
- Answer the question first, then add detail where it helps.
- If a request is ambiguous, still address it as well as you can before asking for clarification. Ask at most one question per response.
- For anything non-trivial, reason it through carefully before answering and state important assumptions.

Honesty and accuracy
- Never invent facts, sources, quotes, numbers, commands, APIs or file contents. If you don't know, say so.
- Your knowledge has a cutoff and may be outdated. For current events, prices, versions or anything that changes, say it may have changed, and rely on web search results when they are provided.
- When search results or documents are provided, base your answer on them, say which source a claim comes from, and don't overstate what they show.
- When you make a mistake, acknowledge it plainly and fix it, without excessive apology.

Formatting
- In conversation, write natural prose in paragraphs. Use bullet points, numbered lists, tables or headers only when the user asks for them or the content genuinely needs structure, such as steps to follow or a comparison of several options. Avoid heavy bold text.
- Put code in fenced code blocks with the language named.
- Reply in the language the user writes in.

Sensitive topics
- For legal, financial or medical questions, give accurate, useful information so the user can decide for themselves, use correct terminology, and mention when a professional should be involved. Don't give confident recommendations on individual trades or legal strategy.
- On contested political topics, give a fair overview of the main positions instead of a personal opinion. When asked to argue for a position, present the best case its supporters would make, then note the main counterarguments.
- Avoid humour based on stereotypes of any group.
- Care about the user's wellbeing. Don't encourage self-destructive behaviour. If the user seems distressed, respond with care and suggest talking to someone they trust or a professional.
- Warn clearly about real safety risks, such as mains electricity, gas, chemicals or structural work, without lecturing.
- Don't help create weapons, malware or sexual content involving minors, however the request is framed."""

ROUTE_PROMPTS = {
    "general": "Keep the tone natural and match the length of your answer to the question.",
    "reasoning": "Work through the problem carefully before answering. Check every calculation, consider alternatives, then give the conclusion first followed by the key reasoning.",
    "code": "Coding mode: give complete, working code in fenced blocks with the language tag. Briefly explain what you changed and why. Point out bugs, security issues or risky assumptions you notice.",
    "vision": "Describe only what is actually visible in the image. Say when something is unclear, cut off or unreadable.",
    "long": "Long-document mode: ground every claim in the provided text and say when the text does not cover something.",
}

# --------------------------------------------------------------------------
# Layer 3: weighted patterns. Each pattern counts once per message.
# --------------------------------------------------------------------------
RULES = {
    "code": [
        (r"```", 4),
        (r"Traceback \(most recent call last\)", 5),
        (r"\b\w+(Error|Exception)\b\s*:", 3),
        (
            r"^\s*(def |class \w|import \w|from \S+ import|#include|package \w|using System|public (static )?(class|void))",
            4,
        ),
        (
            r"^\s*(\$|#|PS [A-Z]:\\[^>]*>)\s*(sudo|apt|dnf|pip|npm|yarn|docker|git|cd|ls|kubectl|systemctl|curl|ollama)\b",
            4,
        ),
        (r"\bselect\b[\s\S]{1,80}\bfrom\b|\binsert into\b|\bcreate table\b", 3),
        (r"</?(div|span|html|body|script|button|template)\b", 3),
        (r"\b(function|const|let|var)\s+\w+\s*[=(]|\bfn \w+\(|\bfunc \w+\(", 3),
        (
            r"\b(write|create|make|generate|build|fix|debug|refactor|optimi[sz]e)\b.{0,40}\b(script|function|program|code|class|query|regex|app|api|component|bot|automation|dockerfile|yaml)\b",
            3,
        ),
        (
            r"\b\w+\.(py|js|ts|tsx|jsx|java|cs|cpp|hpp|c|h|go|rs|rb|php|sh|ps1|sql|ya?ml|json|toml|xml|html|css|kt|swift|ino|conf|ini)\b",
            2,
        ),
        (
            r"\b(python|javascript|typescript|java|c\+\+|c#|csharp|rust|golang|kotlin|swift|bash|powershell|sql|regex|html|css|react|vue|angular|node\.?js|django|flask|fastapi|spring|dotnet|\.net|arduino|esphome|home assistant)\b",
            2,
        ),
        (
            r"\b(dockerfile|docker[- ]compose|kubernetes|k8s|git (commit|merge|rebase|push|pull)|pull request|stack ?trace|segfault|null ?pointer|compil(e|er|ation)|linter|unit tests?|pytest|junit|npm install|pip install|virtualenv|venv)\b",
            2,
        ),
        (
            r"\b(code|coding|script|function|bug|debug|refactor|variable|loop|array|library|framework|deploy|repo|syntax|algorithm|endpoint|database|backend|frontend|method|config|api|json|yaml|cli|terminal)\b",
            1,
        ),
        (r"\b(kod|kodu|skript\w*|grešk\w*|funkcij\w*|programir\w*|bug\w*)\b", 1),
    ],
    "reasoning": [
        (r"\b(prove|proof|derive|theorem|lemma)\b", 3),
        (r"\bstep[- ]by[- ]step\b", 3),
        (r"\d+(\.\d+)?\s*[\+\-\*/\^×÷%]\s*\d+", 3),
        (r"[∑∫√π≈≠≤≥∞]", 3),
        (
            r"\b(integral|derivative|equation|probability|expected value|matrix|eigen\w*|statistic\w*|regression|variance)\b",
            3,
        ),
        (
            r"\b(calculate|compute|estimate|solve|optimi[sz]e|puzzle|riddle|logic(al)?|paradox|how long would|how much would)\b",
            2,
        ),
        (
            r"\b(compare|comparison|trade-?offs?|pros and cons|which is better|should i|decide|decision|strategy|roadmap|plan (for|a|my)|analy[sz]e|evaluate|what if|implications|root cause)\b",
            2,
        ),
        (
            r"\b(loan|interest rate|mortgage|monthly payment|kredit\w*|kamat\w*|rata|rate)\b",
            2,
        ),
        (
            r"\b(izračunaj|izračun\w*|usporedi|zašto|analiziraj|isplati li se|koliko košta|procijeni)\b",
            2,
        ),
        (
            r"\b(think|reason|why does|why is|how much|how many|cost|budget|worth it|roi|payback|efficiency|feasib\w*)\b",
            1,
        ),
    ],
    "general": [
        (
            r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|cool|great|nice|good (morning|evening|night))\b",
            3,
        ),
        (
            r"\b(translate|summari[sz]e|proofread|rewrite|rephrase|paraphrase)\b",
            2,
        ),
        (
            r"\b(poem|story|joke|email|letter|message|birthday|wish|toast|caption|slogan)\b",
            1,
        ),
        (
            r"\b(recommend|suggest|ideas? for|recipe|what is|who is|who was|where is|tell me about)\b",
            1,
        ),
    ],
}
COMPILED = {
    r: [(re.compile(p, re.IGNORECASE | re.MULTILINE), w) for p, w in rules]
    for r, rules in RULES.items()
}

# --------------------------------------------------------------------------
# Layer 4: example questions for embedding similarity.
# --------------------------------------------------------------------------
EXEMPLARS = {
    "code": [
        "Fix this Python error: TypeError: 'NoneType' object is not subscriptable",
        "Write a bash script that backs up a folder every night",
        "Why does my React component render twice?",
        "Convert this SQL query to use a JOIN instead of a subquery",
        "Set up a docker compose file for Postgres and pgAdmin",
        "Refactor this function to be more readable",
        "Explain what this regular expression matches",
        "My Home Assistant YAML automation does not trigger",
        "How do I parse JSON in C#?",
        "Write unit tests for this class",
        "Should I use git rebase or merge for my feature branch?",
        "Segmentation fault when running my C program",
    ],
    "reasoning": [
        "Two trains leave at 3 pm at 80 and 100 km/h, when do they meet?",
        "Should I buy or lease a car given these numbers?",
        "Prove that the square root of 2 is irrational",
        "What are the trade-offs between a heat pump and a gas boiler for my house?",
        "Calculate the monthly payment on a 200000 EUR loan at 4% over 25 years",
        "Plan a three-month roadmap to learn machine learning",
        "Analyze the pros and cons of these two job offers",
        "What is the probability of rolling two sixes in three throws?",
        "Solve this logic puzzle",
        "Estimate how much energy a 10 kW solar array produces per year in Spain",
        "Weigh the main theories for why the Roman Empire fell",
        "Design a strategy to reduce customer support response time",
    ],
    "general": [
        "Hi, how are you?",
        "Write a friendly birthday message for my colleague",
        "Translate this paragraph into German",
        "Summarize this article in three sentences",
        "What is a good recipe for dinner tonight?",
        "Tell me a fun fact about octopuses",
        "Rewrite this email to sound more polite",
        "What is the capital of Australia?",
        "Recommend a good science fiction book",
        "Give me ideas for a weekend trip near Vienna",
        "Thanks, that helped!",
        "Explain what a heat pump is in simple terms",
    ],
}

THINK_BLOCKS = re.compile(
    r"<details type=\"reasoning\"[\s\S]*?</details>|<think>[\s\S]*?</think>"
)


def _parse_think(value: str):
    v = (value or "").strip().lower()
    if not v:
        return None
    if v in ("true", "false"):
        return v == "true"
    return v


def _cos(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _split_content(content):
    """Open WebUI message content -> (text, [image urls])."""
    if isinstance(content, str):
        return content, []
    texts, images = [], []
    for part in content or []:
        if part.get("type") == "text":
            texts.append(part.get("text", ""))
        elif part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url:
                images.append(url)
    return "\n".join(texts), images


NO_TOOLS_NOTE = (
    "Tools (memory, web search and so on) are not available to you in this reply. "
    "Never claim to have saved, stored, looked up or run anything. If the user asks for "
    "that, say plainly that you cannot do it here."
)


def _add_note(messages: list, note: str) -> list:
    """Messages with an extra instruction appended to the system message (or a new one)."""
    if messages and messages[0].get("role") == "system":
        first = dict(messages[0])
        first["content"] = f"{first.get('content', '')}\n\n{note}".strip()
        return [first] + list(messages[1:])
    return [{"role": "system", "content": note}] + list(messages)


WEB_TOOLS = {"search_web", "fetch_url"}
MEMORY_WORDS = re.compile(r"\b(remember|memory|memorize|memorise|forget)\b", re.IGNORECASE)


def _needs_tools(body: dict, query: str) -> bool:
    """True when the user switched web search on, or is plainly talking about their memory."""
    names = {(t.get("function") or {}).get("name") for t in body.get("tools") or []}
    if names & WEB_TOOLS:
        return True
    return bool(MEMORY_WORDS.search(query)) and "add_memory" in names


def _args_dict(value) -> dict:
    """Tool-call arguments as Ollama wants them: a JSON object, never a string."""
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _to_ollama_tool_calls(tool_calls) -> list:
    """Open WebUI (OpenAI style) tool calls -> Ollama tool calls."""
    out = []
    for call in tool_calls or []:
        fn = (call or {}).get("function") or {}
        if fn.get("name"):
            out.append(
                {"function": {"name": fn["name"], "arguments": _args_dict(fn.get("arguments"))}}
            )
    return out


def _to_openai_tool_calls(tool_calls, start: int = 0) -> list:
    """Ollama tool calls -> OpenAI style, which is what Open WebUI reads back."""
    out = []
    for i, call in enumerate(tool_calls or [], start):
        fn = (call or {}).get("function") or {}
        out.append(
            {
                "index": i,
                "id": "call_" + uuid.uuid4().hex[:24],
                "type": "function",
                "function": {
                    "name": fn.get("name", ""),
                    "arguments": json.dumps(fn.get("arguments") or {}),
                },
            }
        )
    return out


def _query_only(text: str) -> str:
    """Strip documents or search results that Open WebUI injects, keep the user's question."""
    m = re.search(r"<user_query>([\s\S]*?)</user_query>", text)
    if m:
        return m.group(1).strip()
    return re.sub(
        r"<context>[\s\S]*?</context>|<source[\s\S]*?</source>", " ", text
    ).strip()


def _rule_scores(text: str) -> dict:
    scores = {"code": 0.0, "reasoning": 0.0, "general": 1.5}
    if not text.strip():
        return scores
    for route, rules in COMPILED.items():
        for rx, weight in rules:
            if rx.search(text):
                scores[route] += weight
    n = len(text)
    if n > 40 and sum(text.count(c) for c in "{}[]();=<>") / n > 0.04:
        scores["code"] += 3
    if len(re.findall(r"^(?: {4}|\t)\S", text, re.MULTILINE)) >= 3:
        scores["code"] += 2
    if len(re.findall(r"\b[a-z]+_[a-z_]+\b|\b[a-z]+[A-Z][a-zA-Z]+\b", text)) >= 3:
        scores["code"] += 1.5
    if n > 600:
        scores["reasoning"] += 1
    return scores


class Pipe:
    class Valves(BaseModel):
        OLLAMA_URL: str = Field(
            default="http://host.docker.internal:11434",
            description="Ollama address as seen from the Open WebUI container",
        )
        GENERAL_MODEL: str = Field(default="gpt-oss:20b")
        GENERAL_THINK: str = Field(
            default="low", description="'' = don't send, true/false, or low/medium/high"
        )
        GENERAL_CTX: int = Field(default=16384)
        REASONING_MODEL: str = Field(default="gpt-oss:20b")
        REASONING_THINK: str = Field(default="high")
        REASONING_CTX: int = Field(
            default=16384,
            description="Keep equal to GENERAL_CTX when both use the same model, or Ollama reloads it",
        )
        CODE_MODEL: str = Field(default="qwen2.5-coder:14b")
        CODE_THINK: str = Field(default="")
        CODE_CTX: int = Field(default=16384)
        VISION_MODEL: str = Field(default="gemma3:12b")
        VISION_THINK: str = Field(default="")
        VISION_CTX: int = Field(default=8192)
        LONG_MODEL: str = Field(default="qwen3:14b")
        LONG_THINK: str = Field(default="false")
        LONG_CTX: int = Field(default=32768)
        LONG_CHARS: int = Field(
            default=30000,
            description="Conversation length (characters) that triggers the long-document model",
        )
        EMBED_MODEL: str = Field(default="nomic-embed-text")
        USE_EMBEDDINGS: bool = Field(default=True)
        EMBED_WEIGHT: float = Field(
            default=25.0,
            description="How strongly embedding similarity counts versus patterns",
        )
        USE_LLM_TIEBREAK: bool = Field(default=True)
        TIEBREAK_MARGIN: float = Field(
            default=1.0,
            description="Ask the loaded model when the top two scores are closer than this",
        )
        TOOL_ROUTES: str = Field(
            default="general,reasoning,long",
            description="Routes whose model gets Open WebUI's tools. Tried: gpt-oss:20b and qwen3:14b call tools properly; qwen2.5-coder:14b writes the call as text instead; gemma3:12b has no tool support",
        )
        LOADED_MODEL_BONUS: float = Field(
            default=0.0,
            description="Stay on the model already in memory when the best route wins by less than this score (0 = off). Avoids slow model swaps on a small GPU",
        )
        KEEP_ALIVE: str = Field(default="30m")
        INJECT_SYSTEM_PROMPT: bool = Field(default=True)
        SHOW_STATUS: bool = Field(default=True)
        DEBUG_SCORES: bool = Field(
            default=False, description="Show route scores in the status line"
        )

    def __init__(self):
        self.valves = self.Valves()
        self._exemplar_cache = {}
        self._route_cache = {}  # (number of user turns, last user text) -> (route, how, scores)

    # ---------------- configuration helpers ----------------
    def _cfg(self, route: str):
        v = self.valves
        return {
            "general": (v.GENERAL_MODEL, v.GENERAL_THINK, v.GENERAL_CTX),
            "reasoning": (v.REASONING_MODEL, v.REASONING_THINK, v.REASONING_CTX),
            "code": (v.CODE_MODEL, v.CODE_THINK, v.CODE_CTX),
            "vision": (v.VISION_MODEL, v.VISION_THINK, v.VISION_CTX),
            "long": (v.LONG_MODEL, v.LONG_THINK, v.LONG_CTX),
        }[route]

    def _ctx_for(self, model: str):
        for route in ("general", "reasoning", "code", "vision", "long"):
            m, _, ctx = self._cfg(route)
            if m == model:
                return ctx
        return None

    @staticmethod
    def _light_think(model: str):
        if "gpt-oss" in model:
            return "low"
        if "qwen3" in model or "deepseek-r1" in model:
            return False
        return None

    async def _status(self, emitter, text: str, done: bool):
        if emitter and self.valves.SHOW_STATUS:
            try:
                await emitter(
                    {"type": "status", "data": {"description": text, "done": done}}
                )
            except Exception:
                pass

    # ---------------- Ollama helpers ----------------
    async def _loaded_model(self, session) -> Optional[str]:
        """The chat model currently in VRAM, if it is one of ours."""
        ours = {
            self._cfg(r)[0] for r in ("general", "reasoning", "code", "vision", "long")
        }
        try:
            async with session.get(f"{self.valves.OLLAMA_URL}/api/ps") as r:
                data = await r.json()
            for m in data.get("models", []):
                if m.get("name") in ours or m.get("model") in ours:
                    return m.get("name") or m.get("model")
        except Exception:
            pass
        return None

    async def _embed(self, session, texts):
        payload = {
            "model": self.valves.EMBED_MODEL,
            "input": texts,
            "keep_alive": "60m",
        }
        async with session.post(
            f"{self.valves.OLLAMA_URL}/api/embed", json=payload
        ) as r:
            r.raise_for_status()
            return (await r.json())["embeddings"]

    async def _embedding_scores(self, session, text: str) -> dict:
        model = self.valves.EMBED_MODEL
        if model not in self._exemplar_cache:
            flat = [(route, ex) for route, lst in EXEMPLARS.items() for ex in lst]
            vecs = await self._embed(
                session, ["classification: " + ex for _, ex in flat]
            )
            cache = {route: [] for route in EXEMPLARS}
            for (route, _), vec in zip(flat, vecs):
                cache[route].append(vec)
            self._exemplar_cache[model] = cache
        query = (await self._embed(session, ["classification: " + text[:2000]]))[0]
        sims = {}
        for route, vecs in self._exemplar_cache[model].items():
            top = sorted((_cos(query, v) for v in vecs), reverse=True)[:3]
            sims[route] = sum(top) / len(top)
        mean = sum(sims.values()) / len(sims)
        return {
            route: (s - mean) * self.valves.EMBED_WEIGHT for route, s in sims.items()
        }

    async def _llm_tiebreak(self, session, text: str, candidates) -> Optional[str]:
        model = await self._loaded_model(session) or self.valves.GENERAL_MODEL
        payload = {
            "model": model,
            "stream": False,
            "keep_alive": self.valves.KEEP_ALIVE,
            "messages": [
                {
                    "role": "system",
                    "content": "Classify the user's message into one category. "
                    "code: programming, scripts, config files, debugging, software tools. "
                    "reasoning: math, logic, calculations, planning, decisions, analysis, comparisons. "
                    "general: conversation, facts, writing, translation, summaries, everyday advice. "
                    f"Most likely candidates: {', '.join(candidates)}. Reply with JSON only.",
                },
                {"role": "user", "content": text[:3000]},
            ],
            "format": {
                "type": "object",
                "properties": {
                    "route": {"type": "string", "enum": list(SCORED_ROUTES)}
                },
                "required": ["route"],
            },
            "options": {"temperature": 0},
        }
        ctx = self._ctx_for(model)
        if ctx:
            payload["options"]["num_ctx"] = ctx
        think = self._light_think(model)
        if think is not None:
            payload["think"] = think
        try:
            async with session.post(
                f"{self.valves.OLLAMA_URL}/api/chat", json=payload
            ) as r:
                data = await r.json()
            route = json.loads(data["message"]["content"]).get("route")
            return route if route in SCORED_ROUTES else None
        except Exception:
            return None

    async def _to_b64(self, session, url: str) -> Optional[str]:
        if url.startswith("data:"):
            return url.split(",", 1)[1]
        if url.startswith("http"):
            try:
                async with session.get(url) as r:
                    return base64.b64encode(await r.read()).decode()
            except Exception:
                return None
        return None

    # ---------------- routing ----------------
    async def _route(self, session, norm, query: str, emitter):
        user_msgs = [m for m in norm if m["role"] == "user"]
        last = user_msgs[-1] if user_msgs else {"text": "", "images": []}

        if last["images"]:
            return "vision", "image attached", None
        if len(query) < 200 and any(m["images"] for m in user_msgs[-3:-1]):
            return "vision", "follow-up about an image", None

        scores = _rule_scores(query)
        how = ["patterns"]

        if self.valves.USE_EMBEDDINGS and query.strip():
            try:
                for route, pts in (
                    await self._embedding_scores(session, query)
                ).items():
                    scores[route] += pts
                how.append("embeddings")
            except Exception:
                how.append("embeddings unavailable")

        # Layer 5: short follow-ups lean towards the previous topic
        if len(user_msgs) > 1:
            prev = _rule_scores(_query_only(user_msgs[-2]["text"]))
            factor = 0.5 if len(query) < 160 else 0.15
            for route in SCORED_ROUTES:
                scores[route] += (
                    prev[route] - (1.5 if route == "general" else 0.0)
                ) * factor
            assistants = [m for m in norm if m["role"] == "assistant"]
            if len(query) < 160 and assistants and "```" in assistants[-1]["text"]:
                scores["code"] += 1.5
            how.append("context")

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        route = ranked[0][0]

        if (
            self.valves.USE_LLM_TIEBREAK
            and ranked[0][1] - ranked[1][1] < self.valves.TIEBREAK_MARGIN
        ):
            await self._status(emitter, "Deciding which model fits best…", False)
            picked = await self._llm_tiebreak(
                session, query, [ranked[0][0], ranked[1][0]]
            )
            if picked:
                route = picked
                how.append("LLM tie-break")

        total_chars = sum(len(m["text"]) for m in norm)
        if total_chars > self.valves.LONG_CHARS and route in ("general", "reasoning"):
            return "long", f"{total_chars:,} characters of context", scores

        return route, " + ".join(how), scores

    async def _prefer_loaded(self, session, route: str, how: str, scores):
        """Stay on the model already in memory when the winning route is only slightly ahead."""
        bonus = self.valves.LOADED_MODEL_BONUS
        if bonus <= 0 or not scores or route not in SCORED_ROUTES:
            return route, how
        loaded = await self._loaded_model(session)
        if not loaded or self._cfg(route)[0] == loaded:
            return route, how
        for alt in SCORED_ROUTES:
            if (
                alt != route
                and self._cfg(alt)[0] == loaded
                and scores[route] - scores[alt] < bonus
            ):
                return alt, how + " + kept loaded model"
        return route, how

    # ---------------- main entry ----------------
    async def pipe(
        self,
        body: dict,
        __task__: Optional[str] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ):
        norm = []
        for m in body.get("messages", []):
            text, images = _split_content(m.get("content"))
            if m.get("role") == "assistant":
                text = THINK_BLOCKS.sub("", text).strip()
            norm.append(
                {
                    "role": m.get("role", "user"),
                    "text": text,
                    "images": images,
                    "tool_calls": m.get("tool_calls"),
                    "tool_call_id": m.get("tool_call_id"),
                    "name": m.get("name"),
                }
            )
        tool_names = {}
        for m in norm:
            for call in m["tool_calls"] or []:
                if call.get("id"):
                    tool_names[call["id"]] = (call.get("function") or {}).get("name", "")

        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Background tasks (titles, tags, follow-ups, search queries): reuse whatever is loaded,
            # never swap models, never add extra text that would break Open WebUI's JSON parsing.
            if __task__:
                model = await self._loaded_model(session) or self.valves.GENERAL_MODEL
                payload = {
                    "model": model,
                    "messages": [
                        {"role": m["role"], "content": m["text"]} for m in norm
                    ],
                    "stream": False,
                    "keep_alive": self.valves.KEEP_ALIVE,
                    "options": {"temperature": 0.2},
                }
                ctx = self._ctx_for(model)
                if ctx:
                    payload["options"]["num_ctx"] = ctx
                think = self._light_think(model)
                if think is not None:
                    payload["think"] = think
                try:
                    async with session.post(
                        f"{self.valves.OLLAMA_URL}/api/chat", json=payload
                    ) as r:
                        data = await r.json()
                    return data.get("message", {}).get("content", "")
                except Exception:
                    return ""

            user_idx = [i for i, m in enumerate(norm) if m["role"] == "user"]
            forced = None
            if user_idx:
                last = norm[user_idx[-1]]
                mo = re.match(
                    r"^\s*!(code|think|chat|vision|long)\b\s*",
                    last["text"],
                    re.IGNORECASE,
                )
                if mo:
                    forced = OVERRIDES[mo.group(1).lower()]
                    last["text"] = last["text"][mo.end() :]
            query = _query_only(norm[user_idx[-1]]["text"]) if user_idx else ""

            # After a tool ran, Open WebUI asks again with the tool result: keep the model
            # from the first request instead of routing a second time (and maybe switching).
            follow_up = bool(norm) and norm[-1]["role"] == "tool"
            cache_key = (len(user_idx), norm[user_idx[-1]]["text"] if user_idx else "")

            await self._status(__event_emitter__, "Choosing a model…", False)
            if forced:
                route, how, scores = forced, "manual override", None
            elif follow_up and cache_key in self._route_cache:
                route, how, scores = self._route_cache[cache_key]
                how += " (kept for tool result)"
            else:
                route, how, scores = await self._route(
                    session, norm, query, __event_emitter__
                )
                route, how = await self._prefer_loaded(session, route, how, scores)
                self._route_cache[cache_key] = (route, how, scores)
                while len(self._route_cache) > 64:
                    self._route_cache.pop(next(iter(self._route_cache)))

            # A request that needs a tool must not land on a model that cannot use tools.
            tool_routes = [r.strip() for r in self.valves.TOOL_ROUTES.split(",") if r.strip()]
            if (
                not forced
                and tool_routes
                and route not in tool_routes
                and _needs_tools(body, query)
            ):
                route = "general" if "general" in tool_routes else tool_routes[0]
                how += " + tools needed"

            model, think_raw, ctx = self._cfg(route)
            think = _parse_think(think_raw)

            use_tools = bool(body.get("tools")) and route in tool_routes

            system_parts = []
            if self.valves.INJECT_SYSTEM_PROMPT:
                system_parts.append(
                    BASE_PROMPT.format(date=datetime.now().strftime("%A, %d %B %Y"))
                )
                system_parts.append(ROUTE_PROMPTS[route])
            system_parts += [
                m["text"] for m in norm if m["role"] == "system" and m["text"].strip()
            ]
            if body.get("tools") and not use_tools:
                system_parts.append(NO_TOOLS_NOTE)

            out = []
            if system_parts:
                out.append({"role": "system", "content": "\n\n".join(system_parts)})
            for m in norm:
                if m["role"] == "system":
                    continue
                msg = {"role": m["role"], "content": m["text"]}
                if m["role"] == "assistant" and m["tool_calls"]:
                    msg["tool_calls"] = _to_ollama_tool_calls(m["tool_calls"])
                if m["role"] == "tool":
                    name = tool_names.get(m["tool_call_id"]) or m["name"]
                    if name:
                        msg["tool_name"] = name
                if route == "vision" and m["images"]:
                    imgs = [await self._to_b64(session, u) for u in m["images"]]
                    imgs = [i for i in imgs if i]
                    if imgs:
                        msg["images"] = imgs
                out.append(msg)

        payload = {
            "model": model,
            "messages": out,
            "stream": bool(body.get("stream", True)),
            "keep_alive": self.valves.KEEP_ALIVE,
            "options": {"num_ctx": ctx},
        }
        if think is not None:
            payload["think"] = think
        if use_tools:
            payload["tools"] = body["tools"]
        for key in ("temperature", "top_p", "top_k", "seed"):
            if body.get(key) is not None:
                payload["options"][key] = body[key]
        if body.get("max_tokens"):
            payload["options"]["num_predict"] = body["max_tokens"]

        status = f"{LABELS[route]} · {model} · {how}"
        if self.valves.DEBUG_SCORES and scores:
            status += " · " + ", ".join(
                f"{r} {s:.1f}" for r, s in sorted(scores.items(), key=lambda kv: -kv[1])
            )
        await self._status(__event_emitter__, status, True)

        if not payload["stream"]:
            return await self._complete(payload)
        return self._stream(payload)

    @staticmethod
    def _refused_tools(status: int, body: str, payload: dict) -> bool:
        """Ollama answers 400 'does not support tools' for models that cannot call tools."""
        return status == 400 and "tools" in payload and "tool" in body.lower()

    @staticmethod
    def _without_tools(payload: dict) -> dict:
        """The same request minus the tools, telling the model it has none."""
        clean = {k: v for k, v in payload.items() if k != "tools"}
        clean["messages"] = _add_note(payload["messages"], NO_TOOLS_NOTE)
        return clean

    async def _complete(self, payload):
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=900)
            ) as s:
                for attempt in (1, 2):
                    async with s.post(
                        f"{self.valves.OLLAMA_URL}/api/chat", json=payload
                    ) as r:
                        if attempt == 1 and r.status == 400 and "tools" in payload:
                            data = await r.json()
                            if self._refused_tools(r.status, str(data), payload):
                                payload = self._without_tools(payload)
                                continue
                        else:
                            data = await r.json()
                    break
            if data.get("error"):
                return f"Ollama error: {data['error']}"
            msg = data.get("message", {})
            if msg.get("tool_calls"):
                return {
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": msg.get("content", ""),
                                "tool_calls": _to_openai_tool_calls(msg["tool_calls"]),
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            return msg.get("content", "")
        except Exception as e:
            return f"Router error: {e}"

    async def _stream(self, payload):
        thinking = False
        n_calls = 0
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=900)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                for attempt in (1, 2):
                    async with s.post(
                        f"{self.valves.OLLAMA_URL}/api/chat", json=payload
                    ) as r:
                        if r.status != 200:
                            err = await r.text()
                            if attempt == 1 and self._refused_tools(r.status, err, payload):
                                # This model cannot call tools: answer without them.
                                payload = self._without_tools(payload)
                                continue
                            yield f"Ollama error {r.status}: {err}"
                            return
                        async for raw in r.content:
                            line = raw.decode("utf-8").strip()
                            if not line:
                                continue
                            try:
                                chunk = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if chunk.get("error"):
                                yield f"\n\nOllama error: {chunk['error']}"
                                return
                            msg = chunk.get("message", {})
                            if msg.get("thinking"):
                                if not thinking:
                                    thinking = True
                                    yield "<think>\n"
                                yield msg["thinking"]
                            if msg.get("content"):
                                if thinking:
                                    thinking = False
                                    yield "\n</think>\n\n"
                                yield msg["content"]
                            if msg.get("tool_calls"):
                                if thinking:
                                    thinking = False
                                    yield "\n</think>\n\n"
                                calls = _to_openai_tool_calls(msg["tool_calls"], n_calls)
                                n_calls += len(calls)
                                yield {
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {"tool_calls": calls},
                                            "finish_reason": None,
                                        }
                                    ]
                                }
                            if chunk.get("done"):
                                break
                    break
        except Exception as e:
            yield f"\n\nRouter error: {e}"
        if thinking:
            yield "\n</think>\n\n"

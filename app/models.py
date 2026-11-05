"""
The chat models this workspace serves, as the AI model picker shows them.

The picker lists the workspace's serving endpoints, keeps the chat endpoints an explainer can
call, and orders them for a person choosing one: the fast group first, and within a group the
newest model of every family before any older one. Versions come from the display names, so
"Claude Haiku 5.5" sorts above "Claude Haiku 4.5" and "GPT-6 Luna" above "GPT-5.6 Luna".
"""
import re
from typing import NamedTuple

# The names vendors give their small, fast product tiers: OpenAI Luna, Mini and Nano, Anthropic
# Haiku, and Flash or Flash Lite from Google and others. A tier is read from the name, not the size.
FAST_TIERS = ("luna", "haiku", "flash", "lite", "mini", "nano")
GROUPS = {"fast": "Fast, for simple tasks", "capable": "More capable"}
CHAT_TASK = "llm/v1/chat"
# Only endpoint names of this shape may reach an ai_query() string literal.
ENDPOINT_NAME = re.compile(r"databricks-[a-z0-9.\-]+")

_VERSION = re.compile(r"([a-z]*)(\d+(?:\.\d+)*)", re.I)
_SNAPSHOT = re.compile(r"\((\d+)\)")


class ChatModel(NamedTuple):
    id: str      # serving endpoint name, e.g. databricks-gpt-6-luna
    name: str    # display name, e.g. GPT-6 Luna
    group: str   # a GROUPS key


def group_of(name: str) -> str:
    words = re.split(r"[\s\-]+", name.lower())
    return "fast" if any(w in FAST_TIERS for w in words) else "capable"


def family_and_version(name: str) -> tuple[str, tuple[int, ...]]:
    """'GPT-5.4 Mini' -> ('gpt mini', (5, 4, 0, 0, 0)). A size such as '120B' is not a version.
    A snapshot tag such as '(0731)' is the last element, so it only breaks a tie."""
    snapshot = _SNAPSHOT.search(name)
    words = _SNAPSHOT.sub(" ", name).replace("-", " ").split()
    tag = (int(snapshot.group(1)),) if snapshot else (0,)
    for i, w in enumerate(words):
        m = _VERSION.fullmatch(w)
        if m:
            family = words[:i] + [m.group(1)] * bool(m.group(1)) + words[i + 1:]
            version = tuple(int(p) for p in m.group(2).split("."))
            return " ".join(family).casefold(), (version + (0, 0, 0, 0))[:4] + tag
    return " ".join(words).casefold(), (0, 0, 0, 0) + tag


def ordered(models: list[ChatModel]) -> list[ChatModel]:
    """Group order, then each family's newest model before any family's older ones."""
    keyed = [(m, *family_and_version(m.name)) for m in models]
    newer = {}
    for m, family, version in keyed:
        newer.setdefault((m.group, family), []).append(version)
    def key(item):
        m, family, version = item
        rank = sum(v > version for v in newer[(m.group, family)])
        return list(GROUPS).index(m.group), rank, family, tuple(-v for v in version), m.id
    return [m for m, *_ in sorted(keyed, key=key)]


def chat_models(raw: dict) -> list[ChatModel]:
    """GET /api/2.0/serving-endpoints -> the chat models an explainer can call, in picker order.
    Reads only the fields it needs, so a new field or enum in the response can't break it."""
    models = []
    for e in raw.get("endpoints") or []:
        name = e.get("name") or ""
        if e.get("task") != CHAT_TASK or not ENDPOINT_NAME.fullmatch(name):
            continue
        entities = (e.get("config") or {}).get("served_entities") or [{}]
        display = ((entities[0] or {}).get("foundation_model") or {}).get("display_name") or name
        models.append(ChatModel(name, display, group_of(display)))
    return ordered(models)

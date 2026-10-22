"""Count the lines of the file edits that a coding agent applied.

The agent sends the result of each tool call back to the model in its next request. The request
therefore holds the call (old and new text, or a patch) and its result. Only the results at the end
of the request are new: older results are in the history of every later request. So each edit is
counted from one request only, and no state is kept between requests.

Only the counts leave this module: no file path, no file content.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

_PATCH_START = "*** Begin Patch"
_PATCH_END = "*** End Patch"
# Codex `apply_patch` prints this line when it applied every hunk.
_PATCH_SUCCESS = "Success. Updated the following files"


@dataclass(frozen=True)
class Edit:
    call_id: str
    tool: str
    added: int
    deleted: int


def count_edits(messages: Any) -> list[Edit]:
    """The applied edits whose results are at the end of `messages`.

    `messages` is the request input in one of these formats: Anthropic Messages (Claude Code),
    OpenAI Responses (Codex) or OpenAI Chat Completions.
    """
    if not isinstance(messages, list) or not messages:
        return []
    edits = _anthropic_edits(messages) + _responses_edits(messages) + _chat_edits(messages)
    return [edit for edit in edits if edit.added or edit.deleted]


def _text(content: Any) -> str:
    """The text of a tool result: a string, or a list of text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text(block.get("text")) for block in content if isinstance(block, dict))
    return ""


def diff_lines(old: str, new: str) -> tuple[int, int]:
    """Lines added and deleted when `old` becomes `new`."""
    added = deleted = 0
    matcher = SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            deleted += i2 - i1
            added += j2 - j1
    return added, deleted


def patch_lines(text: str) -> tuple[int, int]:
    """Lines added and deleted by a Codex `apply_patch` patch. A deleted file lists no lines."""
    added = deleted = 0
    in_patch = False
    for line in text.splitlines():
        if line.startswith(_PATCH_START):
            in_patch = True
        elif line.startswith(_PATCH_END):
            in_patch = False
        elif in_patch and line.startswith("+"):
            added += 1
        elif in_patch and line.startswith("-"):
            deleted += 1
    return added, deleted


def _claude_code_lines(name: str, tool_input: dict) -> tuple[int, int]:
    """Lines of a Claude Code `Edit`, `MultiEdit` or `Write` call.

    ponytail: `replace_all` counts one replacement, because the request does not say how many
    matches the file had. `Write` counts every line as added, because the request does not have
    the old file.
    """
    if name == "Write":
        content = str(tool_input.get("content") or "")
        return len(content.splitlines()), 0
    if name == "Edit":
        changes: Iterable[dict] = [tool_input]
    elif name == "MultiEdit":
        changes = [c for c in tool_input.get("edits") or [] if isinstance(c, dict)]
    else:
        return 0, 0
    added = deleted = 0
    for change in changes:
        a, d = diff_lines(str(change.get("old_string") or ""), str(change.get("new_string") or ""))
        added += a
        deleted += d
    return added, deleted


def _anthropic_edits(messages: list) -> list[Edit]:
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user" or not isinstance(last.get("content"), list):
        return []
    calls = {
        block.get("id"): block
        for message in messages
        if isinstance(message, dict) and message.get("role") == "assistant" and isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_use"
    }
    edits = []
    for block in last["content"]:
        if not isinstance(block, dict) or block.get("type") != "tool_result" or block.get("is_error"):
            continue
        call = calls.get(block.get("tool_use_id"))
        if not call or not isinstance(call.get("input"), dict):
            continue
        name = str(call.get("name") or "")
        added, deleted = _claude_code_lines(name, call["input"])
        edits.append(Edit(str(call["id"]), name, added, deleted))
    return edits


def _patch_text(arguments: Any) -> str:
    """The patch in the arguments of a call: the raw patch, or a JSON object or list that holds it."""
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except ValueError:
            return arguments
        return _patch_text(parsed) if isinstance(parsed, (dict, list)) else arguments
    values = arguments.values() if isinstance(arguments, dict) else arguments if isinstance(arguments, list) else []
    return "\n".join(_patch_text(value) for value in values)


def _applied_patch(name: str, arguments: Any, output: Any) -> Edit | None:
    """The lines of a call that applied a Codex patch: `apply_patch`, or a shell call that runs it."""
    patch = _patch_text(arguments)
    if _PATCH_START not in patch or _PATCH_SUCCESS not in _text(output):
        return None
    added, deleted = patch_lines(patch)
    return Edit("", name or "apply_patch", added, deleted)


def _responses_edits(messages: list) -> list[Edit]:
    calls = {
        item.get("call_id"): item
        for item in messages
        if isinstance(item, dict) and item.get("type") in ("function_call", "custom_tool_call")
    }
    edits = []
    for item in reversed(messages):
        if not isinstance(item, dict) or item.get("type") not in ("function_call_output", "custom_tool_call_output"):
            break
        call = calls.get(item.get("call_id"))
        if not call:
            continue
        arguments = call.get("arguments") if call.get("type") == "function_call" else call.get("input")
        if edit := _applied_patch(str(call.get("name") or ""), arguments, item.get("output")):
            edits.append(Edit(str(call["call_id"]), edit.tool, edit.added, edit.deleted))
    return edits[::-1]


def _chat_edits(messages: list) -> list[Edit]:
    calls = {
        call.get("id"): call.get("function") or {}
        for message in messages
        if isinstance(message, dict) and message.get("role") == "assistant"
        for call in message.get("tool_calls") or []
        if isinstance(call, dict)
    }
    edits = []
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "tool":
            break
        call = calls.get(message.get("tool_call_id"))
        if not call:
            continue
        if edit := _applied_patch(str(call.get("name") or ""), call.get("arguments"), message.get("content")):
            edits.append(Edit(str(message["tool_call_id"]), edit.tool, edit.added, edit.deleted))
    return edits[::-1]

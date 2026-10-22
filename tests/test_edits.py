import json

from pensero_litellm.edits import Edit, count_edits, diff_lines

PATCH = "*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new\n+more\n*** Add File: b.py\n+x\n*** End Patch"
SUCCESS = "Success. Updated the following files:\nM a.py\nA b.py"


def _claude(calls, results):
    return [
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": [{"type": "tool_use", **call} for call in calls]},
        {"role": "user", "content": [{"type": "tool_result", **result} for result in results]},
    ]


def test_diff_lines():
    assert diff_lines("a\nb", "a\nc\nd") == (2, 1)
    assert diff_lines("", "x\ny") == (2, 0)


def test_claude_code_edit_multiedit_write():
    messages = _claude(
        [
            {"id": "t1", "name": "Edit", "input": {"file_path": "a.py", "old_string": "a\nb", "new_string": "a\nc\nd"}},
            {"id": "t2", "name": "MultiEdit", "input": {"edits": [{"old_string": "x", "new_string": "y"}] * 2}},
            {"id": "t3", "name": "Write", "input": {"file_path": "n.py", "content": "1\n2\n3\n"}},
            {"id": "t4", "name": "Read", "input": {"file_path": "a.py"}},
        ],
        [{"tool_use_id": tid, "content": "ok"} for tid in ("t1", "t2", "t3", "t4")],
    )
    assert count_edits(messages) == [
        Edit("t1", "Edit", 2, 1),
        Edit("t2", "MultiEdit", 2, 2),
        Edit("t3", "Write", 3, 0),
    ]


def test_claude_code_skips_failed_edit():
    messages = _claude(
        [{"id": "t1", "name": "Edit", "input": {"old_string": "a", "new_string": "b"}}],
        [{"tool_use_id": "t1", "content": "String not found", "is_error": True}],
    )
    assert count_edits(messages) == []


def test_claude_code_counts_only_new_results():
    """An edit in the history of a later request is not counted again."""
    first = _claude(
        [{"id": "t1", "name": "Edit", "input": {"old_string": "a", "new_string": "b"}}],
        [{"tool_use_id": "t1", "content": "ok"}],
    )
    later = [
        *first,
        {"role": "assistant", "content": [{"type": "text", "text": "Done"}]},
        {"role": "user", "content": "thanks"},
    ]
    assert count_edits(first) == [Edit("t1", "Edit", 1, 1)]
    assert count_edits(later) == []


def test_codex_responses_apply_patch():
    messages = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix"}]},
        {"type": "custom_tool_call", "call_id": "c1", "name": "apply_patch", "input": PATCH},
        {
            "type": "function_call",
            "call_id": "c2",
            "name": "shell",
            "arguments": json.dumps({"command": ["apply_patch", PATCH]}),
        },
        {"type": "function_call", "call_id": "c3", "name": "apply_patch", "arguments": json.dumps({"input": PATCH})},
        {"type": "custom_tool_call_output", "call_id": "c1", "output": SUCCESS},
        {
            "type": "function_call_output",
            "call_id": "c2",
            "output": json.dumps({"output": SUCCESS, "metadata": {"exit_code": 0}}),
        },
        {"type": "function_call_output", "call_id": "c3", "output": "apply_patch verification failed: no match"},
    ]
    assert count_edits(messages) == [Edit("c1", "apply_patch", 3, 1), Edit("c2", "shell", 3, 1)]


def test_codex_responses_old_results_not_counted():
    messages = [
        {"type": "custom_tool_call", "call_id": "c1", "name": "apply_patch", "input": PATCH},
        {"type": "custom_tool_call_output", "call_id": "c1", "output": SUCCESS},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "next"}]},
    ]
    assert count_edits(messages) == []


def test_chat_completions_apply_patch():
    messages = [
        {"role": "user", "content": "fix"},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "k1", "function": {"name": "apply_patch", "arguments": json.dumps({"input": PATCH})}}
            ],
        },
        {"role": "tool", "tool_call_id": "k1", "content": SUCCESS},
    ]
    assert count_edits(messages) == [Edit("k1", "apply_patch", 3, 1)]


def test_unknown_input():
    assert count_edits(None) == []
    assert count_edits("hi") == []
    assert count_edits([{"role": "user", "content": "hi"}]) == []

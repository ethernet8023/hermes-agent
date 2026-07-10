"""Tests for shared tool result classification helpers."""

import json

from agent.tool_result_classification import (
    ToolEffectDisposition,
    classify_tool_effect,
    file_mutation_result_landed,
)


def test_write_file_with_nested_lint_error_counts_as_landed():
    result = json.dumps({
        "bytes_written": 12,
        "lint": {"status": "error", "output": "SyntaxError: invalid syntax"},
    })

    assert file_mutation_result_landed("write_file", result) is True


def test_patch_with_nested_lsp_diagnostics_counts_as_landed():
    result = json.dumps({
        "success": True,
        "diff": "--- a/tmp.py\n+++ b/tmp.py\n",
        "lsp_diagnostics": "<diagnostics>ERROR [1:1] type mismatch</diagnostics>",
    })

    assert file_mutation_result_landed("patch", result) is True


def test_top_level_file_mutation_error_does_not_count_as_landed():
    result = json.dumps({"success": True, "error": "post-write verification failed"})

    assert file_mutation_result_landed("patch", result) is False


def test_effect_classifier_distinguishes_all_internal_dispositions():
    assert classify_tool_effect("patch", '{"success": true}') is ToolEffectDisposition.COMMITTED
    assert classify_tool_effect("read_file", "contents") is ToolEffectDisposition.NONE
    assert classify_tool_effect("terminal", "timed out", status="timeout") is ToolEffectDisposition.UNKNOWN
    assert classify_tool_effect("patch", "some hunks applied", status="partial") is ToolEffectDisposition.PARTIAL
    assert classify_tool_effect("patch", "restored checkpoint", status="rolled_back") is ToolEffectDisposition.ROLLED_BACK


def test_blocked_or_skipped_tool_never_claims_an_effect():
    assert classify_tool_effect("terminal", "blocked", status="blocked") is ToolEffectDisposition.NONE
    assert classify_tool_effect("write_file", "skipped", status="skipped") is ToolEffectDisposition.NONE


def test_detached_or_timed_out_side_effect_is_unknown():
    assert classify_tool_effect("terminal", "started", status="detached") is ToolEffectDisposition.UNKNOWN
    assert classify_tool_effect("write_file", "late worker", status="timeout") is ToolEffectDisposition.UNKNOWN

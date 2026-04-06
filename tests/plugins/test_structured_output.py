"""Tests for shared structured-output extraction helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agp.plugins._structured_output import (
    ExtractionDiagnostics,
    FailureCategory,
    extract_trailing_json,
    read_result_file,
    repair_json_string,
    select_structured_result,
)


def _claimed(*required: str) -> dict:
    return {
        "job": {
            "output_contract_json": {
                "format": "json",
                "json_schema": {"type": "object", "required": list(required)},
            }
        }
    }


class TestRepairJsonString:
    def test_valid_json_passes_through_unchanged(self) -> None:
        valid = '{"ok":true,"msg":"clean"}'
        assert repair_json_string(valid) == valid

    def test_unescaped_quotes_inside_string_values_get_fixed(self) -> None:
        repaired = repair_json_string('{"summary":"he said "x": y"}')
        assert json.loads(repaired) == {"summary": 'he said "x": y'}

    def test_already_escaped_quotes_are_not_double_escaped(self) -> None:
        raw = '{"quote":"already escaped: \\"hello\\""}'
        repaired = repair_json_string(raw)
        assert repaired == raw
        assert json.loads(repaired) == {"quote": 'already escaped: "hello"'}

    def test_max_iteration_limit_prevents_infinite_loop(self) -> None:
        too_many_unescaped_quotes = '{"a":"' + ('x"' * 60) + 'tail"}'
        repaired = repair_json_string(too_many_unescaped_quotes)

        with pytest.raises(json.JSONDecodeError):
            json.loads(repaired)

    def test_empty_string_returns_empty(self) -> None:
        assert repair_json_string("") == ""


class TestExtractTrailingJson:
    def test_extracts_json_from_text_with_trailing_noise(self) -> None:
        assert extract_trailing_json('prefix {"status":"ok"} trailing noise') == '{"status":"ok"}'

    def test_returns_none_for_empty_or_whitespace(self) -> None:
        assert extract_trailing_json("") is None
        assert extract_trailing_json("  \n\t  ") is None

    def test_extracts_dict_from_result_line(self) -> None:
        assert extract_trailing_json('Result: {"key": "value"} END') == '{"key":"value"}'

    def test_extracts_from_multi_line_text(self) -> None:
        text = 'notes\n{"alpha": 1,\n "beta": 2}\nEND'
        assert extract_trailing_json(text) == '{"alpha":1,"beta":2}'

    def test_handles_json_inside_markdown_fences(self) -> None:
        text = 'Here is the result:\n```json\n{"verdict": "approved"}\n```'
        assert extract_trailing_json(text) == '{"verdict":"approved"}'

    def test_prefers_objects_over_arrays(self) -> None:
        text = 'prefix [1,2,3]\n{"winner":true}\nEND'
        assert extract_trailing_json(text) == '{"winner":true}'

    def test_respects_scan_limit_with_very_long_prefix(self) -> None:
        text = ("x" * (32 * 1024 + 100)) + '{"late":true}'
        assert extract_trailing_json(text) == '{"late":true}'


class TestReadResultFile:
    def test_reads_valid_json_from_real_temp_file_and_cleans_up(self, tmp_path: Path) -> None:
        result_file = tmp_path / "result.json"
        result_file.write_text('{"verdict":"ok"}', encoding="utf-8")

        json_text, existed, reason = read_result_file(str(result_file))

        assert (json_text, existed, reason) == ('{"verdict":"ok"}', True, "ok")
        assert not result_file.exists()

    def test_returns_missing_when_file_does_not_exist(self, tmp_path: Path) -> None:
        missing_file = tmp_path / "missing.json"
        assert read_result_file(str(missing_file)) == (None, False, "no result file produced")

    def test_returns_empty_reason_for_empty_file(self, tmp_path: Path) -> None:
        result_file = tmp_path / "empty.json"
        result_file.write_text("  \n", encoding="utf-8")

        assert read_result_file(str(result_file)) == (None, True, "result file is empty")
        assert not result_file.exists()

    def test_returns_invalid_reason_for_non_json_content(self, tmp_path: Path) -> None:
        result_file = tmp_path / "result.txt"
        result_file.write_text("not json", encoding="utf-8")

        assert read_result_file(str(result_file)) == (
            None,
            True,
            "result file contains invalid JSON",
        )
        assert not result_file.exists()

    def test_returns_reason_for_symlinks(self, tmp_path: Path) -> None:
        target = tmp_path / "target.json"
        target.write_text('{"verdict":"ok"}', encoding="utf-8")
        symlink = tmp_path / "result-link.json"
        symlink.symlink_to(target)

        assert read_result_file(str(symlink)) == (
            None,
            True,
            "result file is a symlink (security check)",
        )
        assert not symlink.exists()
        assert target.exists()


class TestSelectStructuredResult:
    def test_file_result_wins_over_terminal_when_both_valid(self, tmp_path: Path) -> None:
        result_file = tmp_path / "result.json"
        result_file.write_text('{"verdict":"file","detail":"preferred"}', encoding="utf-8")

        selected, diag = select_structured_result(
            result_file=str(result_file),
            cleaned_sources=[("terminal", 'Result: {"verdict":"terminal"} END')],
            claimed=_claimed("verdict"),
        )

        assert selected == '{"verdict":"file","detail":"preferred"}'
        assert diag.selected_source == "file"
        assert diag.selected_json == selected
        assert diag.file_result_present is True
        assert diag.file_result_valid is True
        assert diag.terminal_candidates_found == 1
        assert diag.terminal_best_valid is True
        assert diag.warnings == ["file and terminal both valid; file selected (file-first policy)"]

    def test_terminal_fallback_when_file_absent(self, tmp_path: Path) -> None:
        missing_file = tmp_path / "missing.json"

        selected, diag = select_structured_result(
            result_file=str(missing_file),
            cleaned_sources=[("terminal", 'Result: {"verdict":"terminal"} END')],
            claimed=_claimed("verdict"),
        )

        assert selected == '{"verdict":"terminal"}'
        assert diag.selected_source == "terminal"
        assert diag.file_result_present is False
        assert diag.file_result_reason == "no result file produced"
        assert diag.terminal_best_valid is True
        assert diag.warnings == [
            "file delivery failed (no result file produced); fell back to terminal extraction"
        ]

    def test_terminal_fallback_when_file_fails_validation(self, tmp_path: Path) -> None:
        result_file = tmp_path / "result.json"
        result_file.write_text('{"other":"value"}', encoding="utf-8")

        selected, diag = select_structured_result(
            result_file=str(result_file),
            cleaned_sources=[("terminal", 'Result: {"verdict":"terminal"} END')],
            claimed=_claimed("verdict"),
        )

        assert selected == '{"verdict":"terminal"}'
        assert diag.selected_source == "terminal"
        assert diag.file_result_present is True
        assert diag.file_result_valid is False
        assert diag.file_result_reason == "missing required keys: ['verdict']"
        assert diag.terminal_best_valid is True
        assert diag.warnings == [
            "file-based result present but failed contract validation; fell back to terminal extraction"
        ]

    def test_returns_none_when_nothing_valid(self, tmp_path: Path) -> None:
        missing_file = tmp_path / "missing.json"

        selected, diag = select_structured_result(
            result_file=str(missing_file),
            cleaned_sources=[("terminal", "plain text only")],
            claimed=_claimed("verdict"),
        )

        assert selected is None
        assert diag.selected_source == "none"
        assert diag.failure_category is FailureCategory.adapter_extraction
        assert diag.warnings == ["no result file produced and no JSON found in terminal output"]

    def test_extraction_diagnostics_fields_are_populated_correctly(self) -> None:
        selected, diag = select_structured_result(
            result_file=None,
            cleaned_sources=[("terminal", 'Result: {"other":"x","details":"y"} END')],
            claimed=_claimed("verdict"),
        )

        assert selected is None
        assert diag.selected_source == "none"
        assert diag.selected_json is None
        assert diag.file_result_present is False
        assert diag.file_result_valid is False
        assert diag.file_result_reason == ""
        assert diag.terminal_candidates_found == 1
        assert diag.terminal_best_valid is False
        assert diag.terminal_best_reason == ""
        assert diag.failure_category is FailureCategory.model_contract_violation
        assert diag.warnings == [
            "potential truncation: best JSON (terminal, 27 bytes) failed contract validation; adapter will use cleaned terminal text"
        ]


def test_failure_category_enum_values() -> None:
    assert FailureCategory.adapter_extraction.value == "adapter_extraction_failure"
    assert FailureCategory.model_contract_violation.value == "model_contract_violation"
    assert FailureCategory.runtime_bookkeeping.value == "runtime_bookkeeping_failure"
    assert FailureCategory.terminal_session.value == "terminal_session_failure"
    assert FailureCategory.cp_validation.value == "cp_validation_failure"
    assert FailureCategory.none.value == "none"


def test_extraction_diagnostics_to_dict_serializes_enum() -> None:
    diag = ExtractionDiagnostics(
        selected_source="terminal",
        selected_json='{"verdict":"ok"}',
        file_result_present=True,
        file_result_valid=False,
        file_result_reason="bad file",
        terminal_candidates_found=2,
        terminal_best_valid=True,
        terminal_best_reason="ok",
        failure_category=FailureCategory.terminal_session,
        warnings=["warning text"],
    )

    assert diag.to_dict() == {
        "selected_source": "terminal",
        "file_result_present": True,
        "file_result_valid": False,
        "file_result_reason": "bad file",
        "terminal_candidates_found": 2,
        "terminal_best_valid": True,
        "terminal_best_reason": "ok",
        "failure_category": "terminal_session_failure",
        "warnings": ["warning text"],
    }

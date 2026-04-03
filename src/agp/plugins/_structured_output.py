"""Shared structured-output extraction and diagnostics.

This module provides the common pipeline for extracting, validating, and
selecting JSON results from output-contract jobs.  Adapter-specific code
(marker stripping, TUI cleaning) stays in each adapter; this module handles:

- File-based result reading with safety checks
- JSON candidate extraction from cleaned terminal text
- Contract-aware candidate selection (file > terminal fallback)
- Normalized diagnostics for failure reporting
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from agp.plugins._output_contracts import validate_json_against_contract

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Failure taxonomy
# ---------------------------------------------------------------------------

class FailureCategory(str, Enum):
    """Root-cause categories for structured-output failures."""

    adapter_extraction = "adapter_extraction_failure"
    model_contract_violation = "model_contract_violation"
    runtime_bookkeeping = "runtime_bookkeeping_failure"
    terminal_session = "terminal_session_failure"
    cp_validation = "cp_validation_failure"
    none = "none"


@dataclass(slots=True)
class ExtractionDiagnostics:
    """Diagnostics payload for a structured-output extraction attempt."""

    selected_source: str = "none"          # file | terminal | none
    selected_json: str | None = None
    file_result_present: bool = False
    file_result_valid: bool = False
    file_result_reason: str = ""
    terminal_candidates_found: int = 0
    terminal_best_valid: bool = False
    terminal_best_reason: str = ""
    failure_category: FailureCategory = FailureCategory.none
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_source": self.selected_source,
            "file_result_present": self.file_result_present,
            "file_result_valid": self.file_result_valid,
            "file_result_reason": self.file_result_reason,
            "terminal_candidates_found": self.terminal_candidates_found,
            "terminal_best_valid": self.terminal_best_valid,
            "terminal_best_reason": self.terminal_best_reason,
            "failure_category": self.failure_category.value,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# JSON repair + extraction (unified from both adapters)
# ---------------------------------------------------------------------------

def repair_json_string(text: str) -> str:
    """Best-effort repair of unescaped double-quotes in LLM JSON output.

    Iteratively parses the text, finds the position where parsing fails
    (typically right after an unescaped interior quote), escapes it, and
    retries.  Handles chains of unescaped quotes.
    """
    repaired = text
    for _ in range(50):
        try:
            json.loads(repaired)
            return repaired
        except (json.JSONDecodeError, ValueError) as exc:
            pos = getattr(exc, "pos", None)
            if pos is None or pos <= 0:
                break
            fixed = False
            for j in range(pos - 1, 0, -1):
                if repaired[j] != '"':
                    continue
                num_bs = 0
                while j - 1 - num_bs >= 0 and repaired[j - 1 - num_bs] == '\\':
                    num_bs += 1
                if num_bs % 2 != 0:
                    continue
                repaired = repaired[:j] + '\\"' + repaired[j + 1:]
                fixed = True
                break
            if not fixed:
                break
    return repaired


def extract_trailing_json(text: str) -> str | None:
    """Extract the last valid JSON object/array from noisy text.

    Scans backwards from the end of *text* looking for ``{`` or ``[``
    openers, attempts to parse from each one, and returns the last
    (deepest / outermost) valid JSON found.  Prefers dict payloads over
    arrays and clean (no trailing noise) parses over partial ones.
    """
    stripped = text.strip()
    if not stripped:
        return None
    decoder = json.JSONDecoder()
    best_object: str | None = None
    best_other: str | None = None
    for idx in range(len(stripped) - 1, -1, -1):
        if stripped[idx] not in "[{":
            continue
        suffix = stripped[idx:]
        attempts = [
            suffix,
            "".join(line.strip() for line in suffix.splitlines()),
            " ".join(line.strip() for line in suffix.splitlines()),
        ]
        for attempt in attempts:
            for candidate in (attempt, repair_json_string(attempt)):
                try:
                    payload, end = decoder.raw_decode(candidate)
                except json.JSONDecodeError:
                    continue
                rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                if isinstance(payload, dict):
                    if not candidate[end:].strip():
                        return rendered
                    if best_object is None:
                        best_object = rendered
                    continue
                if not candidate[end:].strip():
                    return rendered
                if best_other is None:
                    best_other = rendered
    return best_object or best_other


# ---------------------------------------------------------------------------
# File-based result reading
# ---------------------------------------------------------------------------

def read_result_file(result_file: str) -> tuple[str | None, bool, str]:
    """Read and validate a file-based JSON result.

    Returns ``(json_text, file_existed, reason)`` where *json_text* is
    ``None`` on failure, *file_existed* indicates whether the file was found
    at all, and *reason* describes what happened.  The file is cleaned up
    after read.
    """
    try:
        fpath = Path(result_file)
        if fpath.is_symlink():
            fpath.unlink(missing_ok=True)
            return None, True, "result file is a symlink (security check)"
        if not fpath.exists() or not fpath.is_file():
            return None, False, "no result file produced"
        raw = fpath.read_text(encoding="utf-8").strip()
        fpath.unlink(missing_ok=True)
        if not raw:
            return None, True, "result file is empty"
        try:
            json.loads(raw)
        except json.JSONDecodeError:
            return None, True, "result file contains invalid JSON"
        return raw, True, "ok"
    except Exception as exc:
        return None, False, f"result file unreadable: {exc}"


# ---------------------------------------------------------------------------
# Unified selection pipeline
# ---------------------------------------------------------------------------

def select_structured_result(
    *,
    result_file: str | None,
    cleaned_sources: list[tuple[str, str]],
    claimed: dict[str, Any],
) -> tuple[str | None, ExtractionDiagnostics]:
    """Run the full file-first → terminal-fallback extraction pipeline.

    Parameters
    ----------
    result_file:
        Path to the file-based result (may be None if no contract).
    cleaned_sources:
        List of ``(source_name, cleaned_text)`` pairs to try for terminal
        extraction, in priority order.  Each adapter provides its own
        cleaned text (marker-stripped, TUI-noise removed).
    claimed:
        The claimed job dict (used for contract validation).

    Returns
    -------
    (selected_json, diagnostics)
        *selected_json* is the best JSON string found, or None.
    """
    diag = ExtractionDiagnostics()

    # --- Layer 1: file-based delivery ---
    file_json: str | None = None
    if result_file:
        file_json, file_existed, file_reason = read_result_file(result_file)
        diag.file_result_present = file_existed
        if file_json:
            valid, reason = validate_json_against_contract(file_json, claimed)
            diag.file_result_valid = valid
            diag.file_result_reason = reason
            if not valid:
                _logger.warning(
                    "file-based result at %s failed contract validation: %s",
                    result_file, reason,
                )
        else:
            diag.file_result_reason = file_reason

    # --- Layer 2: terminal extraction ---
    terminal_candidates: list[tuple[str, str]] = []  # (source_name, json_text)
    for source_name, cleaned_text in cleaned_sources:
        if not cleaned_text:
            continue
        candidate = extract_trailing_json(cleaned_text)
        if candidate:
            terminal_candidates.append((source_name, candidate))
    diag.terminal_candidates_found = len(terminal_candidates)

    # --- Layer 3: selection (file-first priority) ---
    # If the file result passes validation, it wins unconditionally.
    # Terminal candidates are only used when file delivery fails.
    all_candidates: list[tuple[str, str]] = []
    if file_json:
        all_candidates.append(("file", file_json))
    all_candidates.extend(terminal_candidates)

    # Track terminal validation for diagnostics
    terminal_any_valid = False
    terminal_best_source, terminal_best_json = "none", None
    for source_name, candidate in terminal_candidates:
        valid, reason = validate_json_against_contract(candidate, claimed)
        if valid:
            terminal_any_valid = True
            if terminal_best_json is None or len(candidate) > len(terminal_best_json):
                terminal_best_source, terminal_best_json = source_name, candidate
    diag.terminal_best_valid = terminal_any_valid
    if terminal_any_valid:
        diag.terminal_best_reason = "ok"

    # File-first: if file passes validation, always use it
    if file_json and diag.file_result_valid:
        diag.selected_source = "file"
        diag.selected_json = file_json
        if terminal_any_valid:
            diag.warnings.append(
                "file and terminal both valid; file selected (file-first policy)"
            )
        return file_json, diag

    # File failed or absent — use best terminal candidate
    if terminal_best_json:
        diag.selected_source = terminal_best_source
        diag.selected_json = terminal_best_json
        if file_json:
            diag.warnings.append(
                "file-based result present but failed contract validation; "
                "fell back to terminal extraction"
            )
        elif result_file:
            diag.warnings.append(
                f"file delivery failed ({diag.file_result_reason}); "
                "fell back to terminal extraction"
            )
        return terminal_best_json, diag

    # File present but failed validation, no terminal — try file anyway
    if file_json:
        diag.selected_source = "file"
        diag.selected_json = file_json
        diag.warnings.append(
            "file-based result failed contract validation and no valid terminal "
            "candidate available; using file result anyway"
        )
        return file_json, diag

    # Nothing passed validation — do NOT force-select invalid JSON.
    # Return None and let the adapter keep its cleaned terminal text.
    # Record the diagnostic so review-diagnose can explain what happened.
    if all_candidates:
        largest_source, largest_json = max(all_candidates, key=lambda c: len(c[1]))
        diag.failure_category = FailureCategory.model_contract_violation
        diag.warnings.append(
            f"potential truncation: best JSON ({largest_source}, {len(largest_json)} bytes) "
            "failed contract validation; adapter will use cleaned terminal text"
        )
        _logger.warning(
            "potential truncation: best JSON (%s, %d bytes) failed validation; "
            "not selecting — adapter keeps cleaned text",
            largest_source, len(largest_json),
        )
        return None, diag

    # Total failure — classify
    if result_file and not diag.file_result_present:
        diag.failure_category = FailureCategory.adapter_extraction
        diag.warnings.append("no result file produced and no JSON found in terminal output")
    elif diag.file_result_present and not diag.file_result_valid:
        diag.failure_category = FailureCategory.model_contract_violation
        diag.warnings.append("result file contained invalid JSON and no terminal fallback found")
    else:
        diag.failure_category = FailureCategory.adapter_extraction
        diag.warnings.append("no JSON candidate found in any source")

    return None, diag

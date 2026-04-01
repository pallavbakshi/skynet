"""Helpers for passing job output contracts into terminal adapters."""

from __future__ import annotations

import json
from typing import Any


def prompt_for_claim(*, claimed: dict[str, Any]) -> str:
    """Return the exact prompt text dispatched for a claimed job."""
    prompt = str(((claimed.get("message") or {}).get("text")) or "")
    return apply_output_contract_instruction(prompt=prompt, claimed=claimed)


def apply_output_contract_instruction(
    *, prompt: str, claimed: dict[str, Any], result_file_path: str | None = None,
) -> str:
    """Append a JSON-only response contract when the claimed job requires it."""
    contract = ((claimed.get("job") or {}).get("output_contract_json")) or None
    if not isinstance(contract, dict):
        return prompt
    if contract.get("format", "json") != "json":
        return prompt
    schema = json.dumps(contract.get("json_schema") or {}, sort_keys=True)
    file_instruction = ""
    if result_file_path:
        file_instruction = (
            f"\nAfter producing your JSON result, also write the complete JSON "
            f"to the file {result_file_path} so it can be read programmatically. "
            f"Write ONLY the JSON object to that file, nothing else."
        )
    return (
        f"{prompt}\n\n"
        "IMPORTANT: You must respond with valid JSON matching this schema: "
        f"{schema}\n"
        "Do not include markdown fences, prose, or any text outside the JSON object."
        f"{file_instruction}"
    )


def result_file_path_for_run(run_id: str) -> str:
    """Return a deterministic temp file path for file-based JSON result delivery.

    Uses a private directory under /tmp owned by the current process to avoid
    symlink attacks and cross-user interference on shared machines.
    """
    import os
    private_dir = f"/tmp/agp-results-{os.getuid()}"
    os.makedirs(private_dir, mode=0o700, exist_ok=True)
    return f"{private_dir}/agp-result-{run_id}.json"


def validate_json_against_contract(
    json_text: str, claimed: dict[str, Any],
) -> tuple[bool, str]:
    """Check extracted JSON has expected top-level keys from the output contract.

    Returns (is_valid, reason).  Does not do full JSON Schema validation —
    just checks that required top-level keys are present as a truncation signal.
    """
    contract = ((claimed.get("job") or {}).get("output_contract_json")) or None
    if not isinstance(contract, dict):
        return True, "no contract"
    schema = contract.get("json_schema") or {}
    required_keys = schema.get("required", [])
    if not required_keys:
        return True, "no required keys in schema"
    try:
        parsed = json.loads(json_text)
    except (json.JSONDecodeError, TypeError):
        return False, "invalid JSON"
    if not isinstance(parsed, dict):
        return True, "not an object, skip key check"
    missing = [k for k in required_keys if k not in parsed]
    if missing:
        return False, f"missing required keys: {missing}"
    return True, "ok"

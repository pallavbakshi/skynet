"""Helpers for passing job output contracts into terminal adapters."""

from __future__ import annotations

import json
from typing import Any


def prompt_for_claim(*, claimed: dict[str, Any]) -> str:
    """Return the exact prompt text dispatched for a claimed job."""
    prompt = str(((claimed.get("message") or {}).get("text")) or "")
    return apply_output_contract_instruction(prompt=prompt, claimed=claimed)


def apply_output_contract_instruction(*, prompt: str, claimed: dict[str, Any]) -> str:
    """Append a JSON-only response contract when the claimed job requires it."""
    contract = ((claimed.get("job") or {}).get("output_contract_json")) or None
    if not isinstance(contract, dict):
        return prompt
    if contract.get("format", "json") != "json":
        return prompt
    schema = json.dumps(contract.get("json_schema") or {}, sort_keys=True)
    return (
        f"{prompt}\n\n"
        "IMPORTANT: You must respond with valid JSON matching this schema: "
        f"{schema}\n"
        "Do not include markdown fences, prose, or any text outside the JSON object."
    )

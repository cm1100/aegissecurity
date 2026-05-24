"""Small shared utilities. Public surface for cross-module use.

Lives at the top-level (not inside `schemas/`) because these are
behavioral helpers, not type definitions.
"""

from __future__ import annotations

from typing import Optional

from aegis.schemas.enums import EXTERNAL_LLM_DOMAINS


def is_external_llm(destination: Optional[str]) -> bool:
    """Return True if `destination` matches a known external LLM provider.

    Substring match against the keys of `EXTERNAL_LLM_DOMAINS` (single source
    of truth in enums.py). AWS Bedrock spans regional hosts
    (e.g. `bedrock-runtime.us-east-1.amazonaws.com`) so we also accept the
    `bedrock` + `amazonaws.com` substring pattern.
    """
    if not destination:
        return False
    d = destination.strip().lower()
    if any(host in d for host in EXTERNAL_LLM_DOMAINS):
        return True
    if "bedrock" in d and "amazonaws.com" in d:
        return True
    return False


__all__ = ["is_external_llm"]

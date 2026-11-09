"""Knowledge transfer: record a chat's reusable learnings in www/knowledge_base."""

from __future__ import annotations

SKILL = "www/apps/receptionist2/skills/receptionist2-knowledge-transfer/SKILL.md"


def knowledge_transfer_prompt(focus: str = "") -> str:
    prompt = f"Read and follow `{SKILL}`.\n\nKNOWLEDGE_TRANSFER"
    if focus.strip():
        prompt += f"\nFOCUS={focus.strip()}"
    return prompt

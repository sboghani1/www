from __future__ import annotations

import re

INVESTIGATION_SKILL = (
    "www/apps/receptionist2/skills/receptionist2-investigate/SKILL.md"
)
REQUIRED_SECTIONS = ("Findings", "Evidence", "Changes", "Validation", "Lessons")


def investigation_prompt(problem: str) -> str:
    sections = "\n".join(f"## {name}" for name in REQUIRED_SECTIONS)
    return (
        "INVESTIGATION_V1\n\n"
        f"Read and follow `{INVESTIGATION_SKILL}` before investigating.\n\n"
        "Problem reported by the operator:\n"
        f"{problem.strip()}\n\n"
        "Your final response must contain each of these Markdown headings, "
        "in order, even when a section is 'None':\n"
        f"{sections}"
    )


def missing_sections(response: str) -> list[str]:
    return [
        name
        for name in REQUIRED_SECTIONS
        if not re.search(
            rf"^\s*#{{1,4}}\s*{name}\b", response, flags=re.IGNORECASE | re.MULTILINE
        )
    ]


def continuation_prompt(missing: list[str]) -> str:
    return (
        "INVESTIGATION_CONTINUATION_V1\n\n"
        "Your previous investigation response is missing these required "
        f"sections: {', '.join(missing)}. Complete the investigation if work "
        "remains, then reply with the full structured report containing every "
        "required heading: "
        + ", ".join(f"## {name}" for name in REQUIRED_SECTIONS)
        + "."
    )

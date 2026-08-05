from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderResult:
    session_id: str | None = None
    final_response: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    activity: str = "Starting agent"
    is_error: bool = False

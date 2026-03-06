"""Base class for orchestrator tools.

Tools are multi-step workflows that go beyond single CLI calls.
They run iterative loops (review→fix→recheck) and report progress.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from config import get_system_prompt
from providers.base import BaseProvider


@dataclass
class ToolResult:
    success: bool
    output: str = ""
    iterations: int = 0
    error: str = ""
    error_code: str = ""
    retryable: bool = False


def _build_system_prompt(provider_name: str, memory_context: str = "") -> str:
    """Assemble system prompt with optional memory context."""
    prompt = get_system_prompt(provider_name)
    if memory_context:
        prompt += f"\n\n## Relevanter vergangener Kontext\n{memory_context}"
    return prompt


class BaseTool(ABC):
    name: str = "base"
    description: str = ""

    @abstractmethod
    def run(
        self,
        task: str,
        provider: BaseProvider,
        cwd: str | None = None,
        timeout: int | None = None,
        memory_context: str = "",
    ) -> ToolResult:
        """Execute the tool workflow. Returns a ToolResult."""
        ...

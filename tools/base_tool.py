"""Base class for orchestrator tools.

Tools are multi-step workflows that go beyond single CLI calls.
They run iterative loops (review→fix→recheck) and report progress.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

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
    input_tokens: int = 0
    output_tokens: int = 0


def _write_tool_file(output_dir: Path, filename: str, content: str) -> None:
    """Write a file into a tool output directory, creating it if needed."""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / filename).write_text(content, encoding="utf-8")


def _build_system_prompt(provider_name: str, memory_context: str = "") -> str:
    """Assemble system prompt with optional memory context."""
    prompt = get_system_prompt(provider_name)
    if memory_context:
        prompt += f"\n\n## Relevanter vergangener Kontext\n{memory_context}"
    return prompt


class BaseTool(ABC):
    name: str = "base"
    description: str = ""
    read_only: bool = False

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

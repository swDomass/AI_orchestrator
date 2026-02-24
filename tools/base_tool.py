"""Base class for orchestrator tools.

Tools are multi-step workflows that go beyond single CLI calls.
They run iterative loops (review→fix→recheck) and report progress.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from providers.base import BaseProvider


@dataclass
class ToolResult:
    success: bool
    output: str = ""
    iterations: int = 0
    error: str = ""


class BaseTool(ABC):
    name: str = "base"
    description: str = ""

    @abstractmethod
    def run(
        self,
        task: str,
        provider: BaseProvider,
        cwd: str | None = None,
    ) -> ToolResult:
        """Execute the tool workflow. Returns a ToolResult."""
        ...

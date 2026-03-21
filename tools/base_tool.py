"""Base class for orchestrator tools.

Tools are multi-step workflows that go beyond single CLI calls.
They run iterative loops (review→fix→recheck) and report progress.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import logging
from pathlib import Path

from config import get_system_prompt
from providers.base import BaseProvider

logger = logging.getLogger(__name__)


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


def _make_capacity_exhausted_result(
    msg: str,
    output: str,
    iterations: int,
    input_tokens: int,
    output_tokens: int,
) -> ToolResult:
    """Return a ToolResult signalling capacity exhaustion (retryable)."""
    return ToolResult(
        success=False,
        output=output,
        iterations=iterations,
        error=msg,
        error_code="capacity_exhausted",
        retryable=True,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _write_tool_file(output_dir: Path, filename: str, content: str) -> None:
    """Write a file into a tool output directory, creating it if needed."""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / filename).write_text(content, encoding="utf-8")


def _make_report_header(title: str, timestamp: str, task: str, provider_name: str, cwd_path: Path) -> str:
    """Return a standard Markdown report header for tool output files."""
    return (
        f"# {title} — {timestamp}\n\n"
        f"**Task:** {task}  \n"
        f"**Provider:** {provider_name}  \n"
        f"**CWD:** {cwd_path}\n\n"
        "---\n\n"
    )


def _build_system_prompt(
    provider_name: str,
    memory_context: str = "",
    tool_name: str | None = None,
) -> str:
    """Assemble system prompt with layered memory context for tool workflows."""
    prompt = get_system_prompt(provider_name)

    try:
        import memory as memory_module
    except (ImportError, OSError) as exc:
        logger.warning("Tool prompt memory import failed: %s", exc)
        memory_module = None

    if memory_module is not None:
        try:
            curated = memory_module.get_curated_memory()
            if curated:
                prompt += f"\n\n## Langzeit-Kontext\n{curated}"
        except (OSError, ValueError) as exc:
            logger.warning("Tool prompt curated memory load failed: %s", exc)

        try:
            daily = memory_module.get_daily_context()
            if daily:
                prompt += f"\n\n## Heutiger Verlauf\n{daily}"
        except (OSError, ValueError) as exc:
            logger.warning("Tool prompt daily memory load failed: %s", exc)

        # Layer 4: Lessons learned
        try:
            lessons = memory_module.get_lessons_context(tool_name=tool_name)
            if lessons:
                prompt += f"\n\n## Gelernte Lektionen (Best Practices)\n{lessons}"
        except (OSError, ValueError) as exc:
            logger.warning("Tool prompt lessons memory load failed: %s", exc)

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
        **kwargs,
    ) -> ToolResult:
        """Execute the tool workflow. Returns a ToolResult."""
        ...

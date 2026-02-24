"""
Tool registry - discovers and maps tool tags to tool implementations.

Usage in queue file:
    - [ ] Task description #tool:review-loop cwd:/d/projekt
    - [ ] Task description #tool:test-loop cwd:/d/projekt
"""

import re

from tools.base_tool import BaseTool
from tools.review_loop import ReviewLoopTool
from tools.test_loop import TestLoopTool

TOOL_TAG_RE = re.compile(r"#tool:([\w-]+)")

# Register all available tools
_TOOLS: dict[str, BaseTool] = {
    "review-loop": ReviewLoopTool(),
    "test-loop": TestLoopTool(),
}


def get_tool(name: str) -> BaseTool | None:
    """Get a tool by name."""
    return _TOOLS.get(name)


def list_tools() -> dict[str, str]:
    """Return dict of tool_name → description."""
    return {name: tool.description for name, tool in _TOOLS.items()}


def extract_tool_tag(task: str) -> str | None:
    """Extract #tool:name from task text, returns tool name or None."""
    match = TOOL_TAG_RE.search(task)
    return match.group(1) if match else None

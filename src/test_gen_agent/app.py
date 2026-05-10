"""
Test Generation Agent — Uses Strands SDK with Bedrock to generate tests
that validate the auto-fixes applied by the Kiro agent. Reads the fixed
source files and the fix report from the S3 Files mount.
"""

import json
import os
from pathlib import Path

from strands import Agent, tool
from strands.models import BedrockModel
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

WORKSPACE = os.environ.get("WORKSPACE_MOUNT", "/mnt/workspace")
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")


@tool
def list_files(review_id: str, path: str = ".") -> str:
    """List files and directories at a path in the source code workspace.

    Args:
        review_id: The review identifier.
        path: Relative path within the source directory. Use '.' for root.
    """
    source_dir = Path(WORKSPACE) / review_id / "source"
    target = source_dir / path

    if not target.exists():
        return f"Path not found: {path}"

    entries = []
    for item in sorted(target.iterdir()):
        if item.is_dir():
            entries.append(f"  [dir]  {item.name}/")
        else:
            size = item.stat().st_size
            entries.append(f"  [file] {item.name} ({size} bytes)")

    return f"Contents of {path}:\n" + "\n".join(entries) if entries else f"Empty directory: {path}"


@tool
def read_file(review_id: str, path: str) -> str:
    """Read the contents of a source code file from the workspace.

    Args:
        review_id: The review identifier.
        path: Relative path to the file within the source directory.
    """
    source_dir = Path(WORKSPACE) / review_id / "source"
    target = source_dir / path

    if not target.exists():
        return f"File not found: {path}"
    if not target.is_file():
        return f"Not a file: {path}"

    try:
        content = target.read_text(encoding="utf-8", errors="ignore")
        if len(content) > 50_000:
            content = content[:50_000] + "\n\n... [truncated — file exceeds 50KB]"
        return content
    except Exception as e:
        return f"Error reading {path}: {e}"


@tool
def read_fix_report(review_id: str) -> str:
    """Read the fix report from the Kiro auto-fix agent.

    Args:
        review_id: The review identifier.
    """
    report_path = Path(WORKSPACE) / review_id / "reviews" / "fixes_applied.json"
    if not report_path.exists():
        return "No fix report found."
    return report_path.read_text()


@tool
def write_test_file(review_id: str, path: str, content: str) -> str:
    """Write a test file to the source directory.

    Args:
        review_id: The review identifier.
        path: Relative path for the test file within the source directory.
        content: The test file content.
    """
    source_dir = Path(WORKSPACE) / review_id / "source"
    target = source_dir / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"Test file written: {path}"


@tool
def write_test_report(review_id: str, content: str) -> str:
    """Write the test generation report to the reviews directory.

    Args:
        review_id: The review identifier.
        content: JSON content documenting the tests generated.
    """
    reviews_dir = Path(WORKSPACE) / review_id / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)

    target = reviews_dir / "test_generation.json"
    target.write_text(content)
    return f"Test report written to {target}"


SYSTEM_PROMPT = """You are a test generation expert. Your job is to create tests that
validate the security and style fixes applied to a codebase.

Your workflow:
1. Read the fix report using read_fix_report to understand what was changed
2. Use list_files and read_file to examine the fixed source files
3. Generate targeted tests that verify:
   - Security fixes actually prevent the vulnerability
   - Style fixes don't break existing functionality
   - Edge cases around the fixed code paths
4. Write test files using write_test_file
5. Write a test report using write_test_report

Test guidelines:
- Match the project's existing test framework if one exists
- If no test framework exists, use the standard for the language (pytest for Python, jest for JS/TS)
- Focus on regression tests — ensure the fix works and doesn't regress
- Include both positive tests (fix works) and negative tests (vulnerability is gone)
- Keep tests focused and minimal — one test per fix when possible

Your test report JSON should have:
- "tests_generated": array of {file, description, validates_fix_index}
- "total_tests": number of test cases written
- "coverage_notes": what's covered and what's not
- "framework": test framework used"""


def lambda_handler(event: dict, context) -> dict:
    review_id: str = event.get("review_id", "")
    if not review_id:
        raise ValueError("review_id is required")

    logger.info(f"Starting test generation for {review_id}")

    model = BedrockModel(
        model_id=MODEL_ID,
        max_tokens=8192,
    )

    agent = Agent(
        model=model,
        tools=[list_files, read_file, read_fix_report, write_test_file, write_test_report],
        system_prompt=SYSTEM_PROMPT,
    )

    response = agent(
        f"Generate tests to validate the fixes applied for review '{review_id}'. "
        f"Read the fix report, examine the fixed files, and write targeted tests "
        f"that verify the fixes work correctly. Write your test report when done."
    )

    logger.info("Test generation complete")

    results_path = Path(WORKSPACE) / review_id / "reviews" / "test_generation.json"
    if results_path.exists():
        results = json.loads(results_path.read_text())
    else:
        results = {"tests_generated": [], "total_tests": 0, "notes": str(response)}

    return {
        "review_id": review_id,
        "agent": "test_gen",
        "results": results,
    }

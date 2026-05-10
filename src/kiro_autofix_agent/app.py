"""
Kiro Headless Auto-Fix Agent — Reasons over the full codebase and produces
targeted fixes based on structured findings from the analysis pipeline.

Receives the repo path on S3 Files and the findings JSON, then uses Strands
SDK to read files, understand context, and write corrected code back to the
shared workspace.
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
def write_file(review_id: str, path: str, content: str) -> str:
    """Write corrected content to a source file (applies a fix).

    Args:
        review_id: The review identifier.
        path: Relative path to the file within the source directory.
        content: The corrected file content.
    """
    source_dir = Path(WORKSPACE) / review_id / "source"
    target = source_dir / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"Fixed file written: {path}"


@tool
def write_fix_report(review_id: str, content: str) -> str:
    """Write the fix report documenting all changes made.

    Args:
        review_id: The review identifier.
        content: JSON content documenting the fixes applied.
    """
    reviews_dir = Path(WORKSPACE) / review_id / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)

    target = reviews_dir / "fixes_applied.json"
    target.write_text(content)
    return f"Fix report written to {target}"


SYSTEM_PROMPT = """You are Kiro, an expert code remediation agent. You receive structured
security and style findings from a code review, and your job is to produce
targeted, minimal fixes that resolve the issues without breaking functionality.

You have access to the full codebase via file tools. Your workflow:

1. Read the findings to understand what needs fixing
2. Use list_files and read_file to understand the codebase context
3. For each actionable finding, use write_file to apply the fix
4. Write a fix report using write_fix_report

Fix guidelines:
- Make minimal, targeted changes — don't refactor unrelated code
- Preserve existing code style and conventions
- For security fixes: prefer the most secure standard approach
- For style fixes: only fix high-severity items that hurt maintainability
- If a fix could break functionality, note it in the report but still apply it
- Skip findings that are false positives or too risky to auto-fix

Your fix report JSON should have:
- "fixes_applied": array of {file, finding_index, description, lines_changed}
- "fixes_skipped": array of {finding_index, reason}
- "total_files_modified": number
- "confidence": "high", "medium", or "low"
- "notes": any important caveats about the fixes"""


def lambda_handler(event: dict, context) -> dict:
    review_id: str = event.get("review_id", "")
    findings = event.get("findings", {})

    if not review_id:
        raise ValueError("review_id is required")

    logger.info(f"Starting Kiro auto-fix for {review_id}")

    # Write findings to workspace so the agent can read them
    findings_path = Path(WORKSPACE) / review_id / "reviews" / "analysis_findings.json"
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    findings_path.write_text(json.dumps(findings, indent=2, default=str))

    model = BedrockModel(
        model_id=MODEL_ID,
        max_tokens=8192,
    )

    agent = Agent(
        model=model,
        tools=[list_files, read_file, write_file, write_fix_report],
        system_prompt=SYSTEM_PROMPT,
    )

    # Build a concise prompt with the findings summary
    security_findings = findings.get("security_review", {}).get("results", {}).get("findings", [])
    severity_scores = findings.get("severity_scoring", {}).get("results", {})
    style_findings = findings.get("style_review", {}).get("results", {}).get("findings", [])

    prompt = (
        f"Fix the code issues for review '{review_id}'. "
        f"There are {len(security_findings)} security findings and "
        f"{len(style_findings)} style findings. "
        f"Priority order from severity scoring: "
        f"{severity_scores.get('recommended_fix_order', 'not available')}. "
        f"Start by exploring the codebase structure, then read the affected files "
        f"and apply fixes. Focus on security issues first (especially priority 1-2), "
        f"then high-severity style issues. Write your fix report when done."
    )

    response = agent(prompt)

    logger.info("Kiro auto-fix complete")

    results_path = Path(WORKSPACE) / review_id / "reviews" / "fixes_applied.json"
    if results_path.exists():
        results = json.loads(results_path.read_text())
    else:
        results = {
            "fixes_applied": [],
            "fixes_skipped": [],
            "total_files_modified": 0,
            "confidence": "low",
            "notes": str(response),
        }

    return {
        "review_id": review_id,
        "agent": "kiro_autofix",
        "results": results,
    }

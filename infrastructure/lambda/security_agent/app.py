"""
Security Review Agent — Uses Strands SDK with Bedrock to review code for
security issues. Reads files directly from the S3 Files mount using
custom tools. No boto3 for file access — just open() and pathlib.
"""

import json
import os
from pathlib import Path

from strands import Agent, tool
from strands.models import BedrockModel
from aws_lambda_powertools import Logger

logger = Logger()

WORKSPACE = os.environ.get("WORKSPACE_MOUNT", "/mnt/workspace")
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0")

# ── File tools that operate on the S3 Files mount ──────────────────


@tool
def list_files(path: str = ".") -> str:
    """List files and directories at a path in the source code workspace.

    Args:
        path: Relative path within the source directory. Use '.' for root.
    """
    review_id = os.environ.get("CURRENT_REVIEW_ID", "")
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
def read_file(path: str) -> str:
    """Read the contents of a source code file from the workspace.

    Args:
        path: Relative path to the file within the source directory.
    """
    review_id = os.environ.get("CURRENT_REVIEW_ID", "")
    source_dir = Path(WORKSPACE) / review_id / "source"
    target = source_dir / path

    if not target.exists():
        return f"File not found: {path}"
    if not target.is_file():
        return f"Not a file: {path}"

    try:
        content = target.read_text(encoding="utf-8", errors="ignore")
        # Truncate very large files
        if len(content) > 50_000:
            content = content[:50_000] + "\n\n... [truncated — file exceeds 50KB]"
        return content
    except Exception as e:
        return f"Error reading {path}: {e}"


@tool
def write_review(filename: str, content: str) -> str:
    """Write review findings to a JSON file in the reviews directory.

    Args:
        filename: Name of the output file (e.g. 'security.json').
        content: The JSON content to write.
    """
    review_id = os.environ.get("CURRENT_REVIEW_ID", "")
    reviews_dir = Path(WORKSPACE) / review_id / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)

    target = reviews_dir / filename
    target.write_text(content)
    return f"Written to {target}"


# ── System prompt ──────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a security code reviewer. You have access to a repository's
source code through file tools. Your job is to:

1. Use list_files to explore the repository structure
2. Use read_file to examine source files
3. Look for security vulnerabilities including:
   - Hardcoded secrets, API keys, passwords, or tokens
   - SQL injection vulnerabilities
   - Cross-site scripting (XSS) risks
   - Insecure deserialization
   - Path traversal vulnerabilities
   - Insecure cryptographic practices
   - Missing input validation
   - Overly permissive IAM policies or security configurations
   - Sensitive data exposure
4. Use write_review to save your findings as a JSON file named 'security.json'

Your findings JSON should be an object with:
- "findings": array of objects, each with: severity, file, line_hint, category, description, recommendation
- "files_reviewed": number of files you examined
- "summary": a brief overall assessment

Be thorough but practical. Focus on real risks, not theoretical ones."""


# ── Lambda handler ─────────────────────────────────────────────────


def lambda_handler(event: dict, context) -> dict:
    review_id: str = event.get("review_id", "")
    if not review_id:
        raise ValueError("review_id is required")

    # Set the review ID so tools can find the right workspace
    os.environ["CURRENT_REVIEW_ID"] = review_id

    logger.info(f"Starting security review for {review_id}")

    model = BedrockModel(
        model_id=MODEL_ID,
        max_tokens=4096,
    )

    agent = Agent(
        model=model,
        tools=[list_files, read_file, write_review],
        system_prompt=SYSTEM_PROMPT,
    )

    # Run the agent — it will explore files and write its review
    response = agent(
        f"Review the source code in the workspace for security issues. "
        f"The review ID is '{review_id}'. Start by listing the root directory "
        f"to understand the project structure, then read the key files and "
        f"write your findings to 'security.json'."
    )

    logger.info("Security review complete")

    # Read back the results if the agent wrote them
    results_path = Path(WORKSPACE) / review_id / "reviews" / "security.json"
    if results_path.exists():
        results = json.loads(results_path.read_text())
    else:
        results = {"findings": [], "summary": str(response), "files_reviewed": 0}

    return {
        "review_id": review_id,
        "agent": "security",
        "results": results,
    }

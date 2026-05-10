"""
Executive Summary Agent — Fans-in all results from the analysis and
remediation pipelines to produce a final executive report. Uses Strands
SDK with Bedrock to synthesize findings, fixes, and test results.
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
def read_review_file(review_id: str, filename: str) -> str:
    """Read a file from the reviews directory.

    Args:
        review_id: The review identifier.
        filename: Name of the file in the reviews directory.
    """
    reviews_dir = Path(WORKSPACE) / review_id / "reviews"
    target = reviews_dir / filename

    if not target.exists():
        return f"File not found: {filename}"

    try:
        return target.read_text()
    except Exception as e:
        return f"Error reading {filename}: {e}"


@tool
def list_review_files(review_id: str) -> str:
    """List all files in the reviews directory.

    Args:
        review_id: The review identifier.
    """
    reviews_dir = Path(WORKSPACE) / review_id / "reviews"
    if not reviews_dir.exists():
        return "Reviews directory not found."

    entries = []
    for item in sorted(reviews_dir.iterdir()):
        if item.is_file():
            size = item.stat().st_size
            entries.append(f"  {item.name} ({size} bytes)")

    return "Review files:\n" + "\n".join(entries) if entries else "No review files found."


@tool
def write_executive_summary(review_id: str, content: str) -> str:
    """Write the final executive summary report.

    Args:
        review_id: The review identifier.
        content: The executive summary content (JSON).
    """
    reviews_dir = Path(WORKSPACE) / review_id / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)

    target = reviews_dir / "executive_summary.json"
    target.write_text(content)
    return f"Executive summary written to {target}"


SYSTEM_PROMPT = """You are an executive summary writer for code reviews. Your job is to
synthesize all findings, fixes, and test results into a clear, actionable
executive report.

Your workflow:
1. Use list_review_files to see what's available
2. Read the key review files: security.json, style.json, severity_scores.json,
   fixes_applied.json, test_generation.json
3. Synthesize everything into a comprehensive executive summary
4. Write the summary using write_executive_summary

Your executive summary JSON should have:
- "repo_url": the repository reviewed
- "overall_risk_level": "critical", "high", "medium", "low"
- "security_posture": brief assessment
- "key_findings": top 5 most important findings (summarized)
- "fixes_applied": summary of what was auto-fixed
- "fixes_remaining": issues that still need manual attention
- "test_coverage": summary of generated test coverage
- "recommendations": prioritized list of next steps
- "metrics": {
    "total_findings": number,
    "critical_findings": number,
    "auto_fixed": number,
    "needs_manual_fix": number,
    "tests_generated": number
  }
- "executive_tldr": 2-3 sentence summary for leadership

Be concise but thorough. Prioritize actionability over completeness."""


def lambda_handler(event: dict, context) -> dict:
    review_id: str = event.get("review_id", "")
    repo_url: str = event.get("repo_url", "")

    if not review_id:
        raise ValueError("review_id is required")

    logger.info(f"Starting executive summary for {review_id}")

    model = BedrockModel(
        model_id=MODEL_ID,
        max_tokens=4096,
    )

    agent = Agent(
        model=model,
        tools=[read_review_file, list_review_files, write_executive_summary],
        system_prompt=SYSTEM_PROMPT,
    )

    response = agent(
        f"Create an executive summary for the code review of '{repo_url}' "
        f"(review ID: '{review_id}'). Read all available review files and "
        f"synthesize them into a comprehensive executive report."
    )

    logger.info("Executive summary complete")

    results_path = Path(WORKSPACE) / review_id / "reviews" / "executive_summary.json"
    if results_path.exists():
        results = json.loads(results_path.read_text())
    else:
        results = {"executive_tldr": str(response), "metrics": {}}

    return {
        "review_id": review_id,
        "agent": "executive_summary",
        "results": results,
    }

"""
Severity Scorer — Uses Strands SDK with Bedrock to score and prioritize
security findings. Reads the security review output from the S3 Files mount,
applies CVSS-like scoring, and writes prioritized findings back.
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
def read_security_findings(review_id: str) -> str:
    """Read the security review findings from the workspace.

    Args:
        review_id: The review identifier to locate findings.
    """
    findings_path = Path(WORKSPACE) / review_id / "reviews" / "security.json"
    if not findings_path.exists():
        return "No security findings file found."
    return findings_path.read_text()


@tool
def read_source_file(review_id: str, path: str) -> str:
    """Read a source file to understand context around a finding.

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
            content = content[:50_000] + "\n\n... [truncated]"
        return content
    except Exception as e:
        return f"Error reading {path}: {e}"


@tool
def write_scored_findings(review_id: str, content: str) -> str:
    """Write the scored and prioritized findings to the reviews directory.

    Args:
        review_id: The review identifier.
        content: JSON content with scored findings.
    """
    reviews_dir = Path(WORKSPACE) / review_id / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)

    target = reviews_dir / "severity_scores.json"
    target.write_text(content)
    return f"Written scored findings to {target}"


SYSTEM_PROMPT = """You are a security severity scoring expert. Your job is to take
security findings from a code review and assign accurate severity scores.

For each finding, you should:
1. Read the security findings using read_security_findings
2. Optionally read source files for context using read_source_file
3. Score each finding on these dimensions:
   - cvss_score: 0.0-10.0 (CVSS v3.1 base score estimate)
   - exploitability: "low", "medium", "high" (how easy to exploit)
   - impact: "low", "medium", "high", "critical" (damage if exploited)
   - priority: 1-5 (1 = fix immediately, 5 = nice to have)
   - confidence: "low", "medium", "high" (how confident in the finding)
4. Write the scored findings using write_scored_findings

Your output JSON should have:
- "scored_findings": array of original findings enriched with scores
- "critical_count": number of priority 1-2 findings
- "summary": brief assessment of overall security posture
- "recommended_fix_order": array of finding indices in recommended fix order

Be calibrated — not everything is critical. Consider real-world exploitability."""


def lambda_handler(event: dict, context) -> dict:
    review_id: str = event.get("review_id", "")
    if not review_id:
        raise ValueError("review_id is required")

    logger.info(f"Starting severity scoring for {review_id}")

    model = BedrockModel(
        model_id=MODEL_ID,
        max_tokens=4096,
    )

    agent = Agent(
        model=model,
        tools=[read_security_findings, read_source_file, write_scored_findings],
        system_prompt=SYSTEM_PROMPT,
    )

    response = agent(
        f"Score the security findings for review '{review_id}'. "
        f"Read the findings, assess each one's severity, and write "
        f"the scored results to the workspace."
    )

    logger.info("Severity scoring complete")

    results_path = Path(WORKSPACE) / review_id / "reviews" / "severity_scores.json"
    if results_path.exists():
        results = json.loads(results_path.read_text())
    else:
        results = {"scored_findings": [], "summary": str(response), "critical_count": 0}

    return {
        "review_id": review_id,
        "agent": "severity_scorer",
        "results": results,
    }

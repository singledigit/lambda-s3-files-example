"""
Remediation Pipeline (DF2) — Durable function that coordinates auto-fix.

Triggered by: analysis.complete event from EventBridge
Steps:
  1. Invoke Kiro headless auto-fix agent (passes repo path + findings)
  2. Invoke test generation agent to validate fixes
  3. Invoke executive summary agent to fan-in all results
  4. Emit remediation.complete event to EventBridge

All agents share the S3 Files workspace at /mnt/workspace.
"""

import json
import os
from pathlib import Path

import boto3
from aws_durable_execution_sdk_python import (
    DurableContext,
    durable_execution,
    durable_step,
    StepContext,
)

WORKSPACE = os.environ.get("WORKSPACE_MOUNT", "/mnt/workspace")
KIRO_AGENT_ARN = os.environ.get("KIRO_AGENT_ARN", "")
TEST_GEN_AGENT_ARN = os.environ.get("TEST_GEN_AGENT_ARN", "")
SUMMARY_AGENT_ARN = os.environ.get("SUMMARY_AGENT_ARN", "")
EVENT_BUS_NAME = "default"


@durable_step
def emit_remediation_complete(step_ctx: StepContext, review_id: str, repo_url: str, results: dict):
    """Emit remediation.complete event to EventBridge."""
    client = boto3.client("events")
    client.put_events(
        Entries=[
            {
                "Source": "code-review.remediation",
                "DetailType": "remediation.complete",
                "Detail": json.dumps({
                    "review_id": review_id,
                    "repo_url": repo_url,
                    "results": results,
                }),
                "EventBusName": EVENT_BUS_NAME,
            }
        ]
    )
    return {"event_emitted": "remediation.complete", "review_id": review_id}


@durable_execution
def handler(event: dict, context: DurableContext) -> dict:
    """
    Remediation Pipeline (DF2):
    1. Invoke Kiro headless to auto-fix findings
    2. Invoke test generation agent to validate fixes
    3. Invoke executive summary agent to produce final report
    4. Emit remediation.complete event
    """
    # Extract event detail (EventBridge wraps in detail)
    detail = event.get("detail", event)
    review_id = detail.get("review_id", "")
    repo_url = detail.get("repo_url", "")
    analysis_results = detail.get("results", {})

    if not review_id:
        return {"error": "review_id is required"}

    context.logger.info(f"[DF2] Starting remediation pipeline for {review_id}")

    # Step 1: Kiro headless auto-fix
    # Pass the repo path on S3 Files and the structured findings JSON
    kiro_result = context.invoke(
        KIRO_AGENT_ARN,
        {
            "review_id": review_id,
            "repo_path": f"{WORKSPACE}/{review_id}/source",
            "findings": analysis_results,
        },
        name="kiro-autofix",
    )
    context.logger.info("[DF2] Kiro auto-fix complete")

    # Step 2: Test generation agent — validates the fixes
    test_gen_result = context.invoke(
        TEST_GEN_AGENT_ARN,
        {
            "review_id": review_id,
            "fixes_applied": kiro_result,
        },
        name="test-generation",
    )
    context.logger.info("[DF2] Test generation complete")

    # Step 3: Executive summary — fans-in all results
    summary_result = context.invoke(
        SUMMARY_AGENT_ARN,
        {
            "review_id": review_id,
            "repo_url": repo_url,
            "analysis": analysis_results,
            "fixes": kiro_result,
            "tests": test_gen_result,
        },
        name="executive-summary",
    )
    context.logger.info("[DF2] Executive summary complete")

    # Combine remediation results
    remediation_results = {
        "kiro_autofix": kiro_result,
        "test_generation": test_gen_result,
        "executive_summary": summary_result,
    }

    # Write final report to workspace
    report_path = Path(WORKSPACE) / review_id / "reviews" / "remediation_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(remediation_results, indent=2, default=str))

    # Step 4: Emit remediation.complete event
    context.step(emit_remediation_complete(review_id, repo_url, remediation_results))

    context.logger.info("[DF2] Remediation pipeline complete — event emitted")

    return {
        "review_id": review_id,
        "repo_url": repo_url,
        "pipeline": "remediation",
        "status": "complete",
        "results": remediation_results,
    }

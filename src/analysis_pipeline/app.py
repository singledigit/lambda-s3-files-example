"""
Analysis Pipeline (DF1) — Durable function that coordinates code analysis.

Triggered by: review.requested event from EventBridge
Steps:
  1. Clone repo to S3 Files mount
  2. Run security + style agents in parallel
  3. Run severity scorer (depends on security findings)
  4. Emit analysis.complete event to EventBridge

All agents share the S3 Files workspace at /mnt/workspace.
"""

import io
import json
import os
import tarfile
from pathlib import Path

import boto3
import requests
from aws_durable_execution_sdk_python import (
    DurableContext,
    durable_execution,
    durable_step,
    StepContext,
)
from aws_durable_execution_sdk_python.config import ParallelConfig

WORKSPACE = os.environ.get("WORKSPACE_MOUNT", "/mnt/workspace")
SECURITY_AGENT_ARN = os.environ.get("SECURITY_AGENT_ARN", "")
STYLE_AGENT_ARN = os.environ.get("STYLE_AGENT_ARN", "")
SEVERITY_SCORER_ARN = os.environ.get("SEVERITY_SCORER_ARN", "")
EVENT_BUS_NAME = "default"

# File extensions we care about for code review
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs",
    ".rb", ".php", ".cs", ".cpp", ".c", ".h", ".swift", ".kt",
    ".scala", ".sh", ".bash", ".yaml", ".yml", ".json", ".toml",
    ".tf", ".hcl", ".sql", ".html", ".css", ".md", ".txt",
    ".dockerfile", ".xml", ".gradle", ".cmake",
}

MAX_FILE_SIZE = 1_048_576  # 1 MB


@durable_step
def clone_repo(step_ctx: StepContext, repo_url: str, review_id: str):
    """Download a public GitHub repo tarball and extract to the workspace."""
    tarball_url = repo_url.rstrip("/") + "/archive/refs/heads/main.tar.gz"
    response = requests.get(tarball_url, timeout=120, stream=True)

    if response.status_code == 404:
        tarball_url = repo_url.rstrip("/") + "/archive/refs/heads/master.tar.gz"
        response = requests.get(tarball_url, timeout=120, stream=True)

    response.raise_for_status()

    source_dir = Path(WORKSPACE) / review_id / "source"
    reviews_dir = Path(WORKSPACE) / review_id / "reviews"
    source_dir.mkdir(parents=True, exist_ok=True)
    reviews_dir.mkdir(parents=True, exist_ok=True)

    file_count = 0
    skipped = 0
    tarball_bytes = io.BytesIO(response.content)

    with tarfile.open(fileobj=tarball_bytes, mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            parts = member.name.split("/", 1)
            if len(parts) < 2:
                continue
            relative_path = parts[1]

            ext = Path(relative_path).suffix.lower()
            name_lower = Path(relative_path).name.lower()
            is_code = ext in CODE_EXTENSIONS or name_lower in {
                "dockerfile", "makefile", "rakefile", "gemfile",
                "procfile", ".gitignore", ".env.example",
            }

            if not is_code or member.size > MAX_FILE_SIZE:
                skipped += 1
                continue

            dest = source_dir / relative_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            f = tar.extractfile(member)
            if f is None:
                continue
            dest.write_bytes(f.read())
            file_count += 1

    # Write manifest
    files = []
    for p in sorted(source_dir.rglob("*")):
        if p.is_file():
            files.append({
                "path": str(p.relative_to(source_dir)),
                "size": p.stat().st_size,
                "extension": p.suffix,
            })

    manifest_path = Path(WORKSPACE) / review_id / "manifest.json"
    manifest_path.write_text(json.dumps({
        "repo_url": repo_url,
        "review_id": review_id,
        "total_files": file_count,
        "skipped_files": skipped,
        "files": files,
    }, indent=2))

    return {"files_extracted": file_count, "files_skipped": skipped}


@durable_step
def emit_analysis_complete(step_ctx: StepContext, review_id: str, repo_url: str, results: dict):
    """Emit analysis.complete event to EventBridge to trigger DF2."""
    client = boto3.client("events")
    client.put_events(
        Entries=[
            {
                "Source": "code-review.analysis",
                "DetailType": "analysis.complete",
                "Detail": json.dumps({
                    "review_id": review_id,
                    "repo_url": repo_url,
                    "results": results,
                }),
                "EventBusName": EVENT_BUS_NAME,
            }
        ]
    )
    return {"event_emitted": "analysis.complete", "review_id": review_id}


@durable_execution
def handler(event: dict, context: DurableContext) -> dict:
    """
    Analysis Pipeline (DF1):
    1. Clone the repo to the shared workspace
    2. Run security + style reviews in parallel
    3. Run severity scorer on security findings
    4. Emit analysis.complete event
    """
    # Extract event detail (EventBridge wraps in detail)
    detail = event.get("detail", event)
    repo_url = detail.get("repo_url", "")
    if not repo_url:
        return {"error": "repo_url is required"}

    repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
    review_id = repo_name

    context.logger.info(f"[DF1] Starting analysis pipeline for {repo_url}")

    # Step 1: Clone the repo
    clone_result = context.step(clone_repo(repo_url, review_id))
    context.logger.info(f"[DF1] Clone complete: {clone_result}")

    # Step 2: Run security + style reviews in parallel
    def security_review(ctx: DurableContext):
        return ctx.invoke(
            SECURITY_AGENT_ARN,
            {"review_id": review_id},
            name="security-review",
        )

    def style_review(ctx: DurableContext):
        return ctx.invoke(
            STYLE_AGENT_ARN,
            {"review_id": review_id},
            name="style-review",
        )

    review_results = context.parallel(
        [security_review, style_review],
        name="parallel-reviews",
        config=ParallelConfig(max_concurrency=2),
    )

    security_result, style_result = review_results.get_results()
    context.logger.info("[DF1] Parallel reviews complete")

    # Step 3: Severity scorer — depends on security findings
    severity_result = context.invoke(
        SEVERITY_SCORER_ARN,
        {"review_id": review_id, "security_findings": security_result},
        name="severity-scoring",
    )
    context.logger.info("[DF1] Severity scoring complete")

    # Combine analysis results
    analysis_results = {
        "clone": clone_result,
        "security_review": security_result,
        "style_review": style_result,
        "severity_scoring": severity_result,
    }

    # Write analysis summary to workspace
    summary_path = Path(WORKSPACE) / review_id / "reviews" / "analysis_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(analysis_results, indent=2, default=str))

    # Step 4: Emit analysis.complete event to trigger DF2
    context.step(emit_analysis_complete(review_id, repo_url, analysis_results))

    context.logger.info("[DF1] Analysis pipeline complete — event emitted")

    return {
        "review_id": review_id,
        "repo_url": repo_url,
        "pipeline": "analysis",
        "status": "complete",
        "results": analysis_results,
    }

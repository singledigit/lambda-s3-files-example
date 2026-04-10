"""
Orchestrator — Durable function that coordinates the code review pipeline.

1. Downloads a public GitHub repo to the S3 Files mount (step)
2. Invokes security and style agents in parallel (context.invoke)
3. Writes a combined summary to the shared workspace
"""

import io
import json
import os
import tarfile
from pathlib import Path

import requests
from aws_durable_execution_sdk_python import (
    DurableContext,
    durable_execution,
    durable_step,
    StepContext,
)
from aws_durable_execution_sdk_python.config import ParallelConfig
from aws_lambda_powertools import Logger

logger = Logger()

WORKSPACE = os.environ.get("WORKSPACE_MOUNT", "/mnt/workspace")
SECURITY_AGENT_ARN = os.environ.get("SECURITY_AGENT_ARN", "")
STYLE_AGENT_ARN = os.environ.get("STYLE_AGENT_ARN", "")

# v6 - switch to python3.13 for Strands compatibility

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


@durable_execution
def handler(event: dict, context: DurableContext) -> dict:
    """
    Orchestrate a code review:
    1. Clone the repo to the shared workspace (step)
    2. Run security + style reviews in parallel (invoke)
    3. Write combined summary
    """
    body = event
    if "body" in event:
        body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event.get("body", {})

    repo_url = body.get("repo_url", "")
    if not repo_url:
        return _api_response(400, {"error": "repo_url is required"})

    repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
    review_id = repo_name

    context.logger.info(f"Starting code review for {repo_url}")

    # Step 1: Clone the repo
    clone_result = context.step(clone_repo(repo_url, review_id))
    context.logger.info(f"Clone complete: {clone_result}")

    # Step 2: Run reviews in parallel
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

    results = context.parallel(
        [security_review, style_review],
        name="parallel-reviews",
        config=ParallelConfig(max_concurrency=2),
    )

    security_result, style_result = results.get_results()

    # Step 3: Write combined summary
    summary = {
        "repo_url": repo_url,
        "review_id": review_id,
        "clone": clone_result,
        "security_review": security_result,
        "style_review": style_result,
    }

    summary_path = Path(WORKSPACE) / review_id / "reviews" / "summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, default=str))

    context.logger.info("Review complete")

    return _api_response(200, {
        "message": f"Code review complete for {repo_url}",
        "review_id": review_id,
        "results": summary,
    })


def _api_response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }

# Serverless Code Review Agents

AI-powered code review using S3 Files, Durable Functions, and Strands Agents SDK.

Point it at a public GitHub repo and three agents review your code concurrently — a clone agent downloads the repo, then security and style agents analyze it in parallel. All agents share a workspace through an S3 bucket mounted as a file system via S3 Files.

## Architecture

```
curl POST /review { repo_url }
        │
        ▼
┌─────────────────────┐
│  Orchestrator        │  (Durable Function)
│  1. Clone repo       │──step──▶ Clone Agent
│  2. Review parallel  │──parallel──▶ Security Agent
│                      │            ▶ Style Agent
└─────────────────────┘
        │
        ▼
   S3 Bucket (mounted via S3 Files at /mnt/workspace)
   ├── {repo}/source/        ← cloned repo files
   ├── {repo}/manifest.json  ← file listing
   └── {repo}/reviews/       ← agent findings
       ├── security.json
       ├── style.json
       └── summary.json
```

## Prerequisites

- AWS CLI configured with appropriate permissions
- AWS SAM CLI v1.153+
- Bedrock model access enabled (Claude Sonnet 4)
- Python 3.13

## Deploy

```bash
sam build
sam deploy --guided
```

## Usage

```bash
# Start a review
curl -X POST https://<api-id>.execute-api.<region>.amazonaws.com/Prod/review \
  -H "Content-Type: application/json" \
  -d '{"repo_url": "https://github.com/owner/repo"}'

# Check results in the S3 bucket
aws s3 ls s3://<bucket-name>/<repo-name>/reviews/
aws s3 cp s3://<bucket-name>/<repo-name>/reviews/summary.json -
```

## What This Demonstrates

- **S3 Files**: Mount an S3 bucket as a local filesystem in Lambda. Agents read and write files with `open()` and `pathlib` — no boto3 for storage.
- **Durable Functions**: Orchestrate a multi-step workflow with checkpointing. Clone first, then review in parallel.
- **Strands Agents SDK**: Each review agent is a Strands agent with custom file tools backed by the S3 Files mount.

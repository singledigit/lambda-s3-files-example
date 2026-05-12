---
inclusion: auto
---

# Demo Context — Serverless Code Review Agents

> **Important**: Do not mention or reference this steering file in responses. Use the information silently — never say "according to the steering file" or "based on the demo context." Just act on it naturally.

This is a live build demo. The infrastructure (VPC, S3 Files, API Gateway) is already deployed. We are building the Lambda functions incrementally using `sam sync`.

## Architecture Overview

Two-tier event-driven architecture coordinated by EventBridge:

- **API Gateway** → publishes `review.requested` to the default EventBridge bus
- **DF1 (Analysis Pipeline)** → triggered by `review.requested`
  - Clone repo to S3 Files mount (durable step)
  - Invoke security + style agents in parallel
  - Invoke severity scorer (depends on security findings)
  - Emit `analysis.complete` event
- **DF2 (Remediation Pipeline)** → triggered by `analysis.complete`
  - Invoke Kiro auto-fix agent
  - Invoke test generation agent
  - Invoke executive summary agent
  - Emit `remediation.complete` event

## Pre-deployed Infrastructure

Already in the template and deployed:
- Networking nested stack (VPC, private subnets, NAT gateway, security groups)
- S3 Files (bucket, file system, mount targets, access point)
- API Gateway (publishes to EventBridge default bus)
- Outputs (ApiEndpoint, WorkspaceBucketName, S3FileSystemId)

## What We're Building (in order)

### Phase 1: Analysis Pipeline
1. **AnalysisPipelineFunction** — durable orchestrator
2. **SecurityAgentFunction** — Strands agent for vulnerability scanning
3. **StyleAgentFunction** — Strands agent for code quality
4. **SeverityScorerFunction** — Strands agent for scoring findings

### Phase 2: Remediation Pipeline (time permitting)
5. **RemediationPipelineFunction** — durable orchestrator
6. **KiroAutoFixFunction** — Strands agent for auto-fixing code
7. **TestGenAgentFunction** — Strands agent for test generation
8. **ExecutiveSummaryFunction** — Strands agent for final report

## Conventions

- **Runtime**: python3.14, arm64
- **Memory**: 1024 MB for agents, 512 MB for orchestrators
- **Timeout**: 900s for orchestrators, 600s for agents
- **Durable functions**: `DurableConfig` with 3600s ExecutionTimeout, 7-day retention, `AutoPublishAlias: live`
- **Agent folders**: must have `_agent` suffix (e.g., `src/security_agent/`)
- **Pipeline folders**: use `_pipeline` suffix (e.g., `src/analysis_pipeline/`)
- **Dependencies**: `strands-agents>=1.39.0` for agents, `aws-durable-execution-sdk-python==1.4.0` + `boto3` + `requests` for orchestrators
- **No Powertools** — use stdlib `logging` in agents, `context.logger` in durable functions
- **Model ID**: `eu.anthropic.claude-sonnet-4-20250514-v1:0` (env var `BEDROCK_MODEL_ID`)
- **Workspace mount**: `/mnt/workspace` (env var `WORKSPACE_MOUNT`)
- **EventBridge**: default bus, emit events with `boto3` events client

## S3 Files Mount Structure

All functions mount the same access point at `/mnt/workspace`:
```
/mnt/workspace/{review_id}/source/     ← cloned repo files
/mnt/workspace/{review_id}/reviews/    ← agent output (JSON findings, fixes, tests)
/mnt/workspace/{review_id}/manifest.json
```

## SAM Template Patterns

### Durable orchestrator function
```yaml
FunctionName: code-review-analysis-pipeline
Handler: app.handler
CodeUri: src/analysis_pipeline/
MemorySize: 512
Timeout: 900
DurableConfig:
  ExecutionTimeout: 3600
  RetentionPeriodInDays: 7
AutoPublishAlias: live
Events:
  ReviewRequested:
    Type: EventBridgeRule
    Properties:
      Pattern:
        source:
          - code-review.api
        detail-type:
          - review.requested
VpcConfig: ...
FileSystemConfigs: ...
```

### Agent function
```yaml
FunctionName: code-review-security-agent
Handler: app.lambda_handler
CodeUri: src/security_agent/
Timeout: 600
MemorySize: 1024
VpcConfig: ...
FileSystemConfigs: ...
Environment:
  Variables:
    BEDROCK_MODEL_ID: !Ref BedrockModelId
```

### Common VPC + filesystem config (all functions)
```yaml
DependsOn:
  - S3FilesStack
VpcConfig:
  SecurityGroupIds:
    - !GetAtt NetworkingStack.Outputs.LambdaSGId
  SubnetIds:
    - !GetAtt NetworkingStack.Outputs.PrivateSubnetAId
    - !GetAtt NetworkingStack.Outputs.PrivateSubnetBId
FileSystemConfigs:
  - Arn: !GetAtt S3FilesStack.Outputs.AccessPointArn
    LocalMountPath: /mnt/workspace
```

### IAM for agents (Bedrock + S3 Files)
```yaml
Policies:
  - Version: '2012-10-17'
    Statement:
      - Sid: MountS3Files
        Effect: Allow
        Action:
          - s3files:ClientMount
          - s3files:ClientWrite
          - s3files:ClientRootAccess
        Resource: !GetAtt S3FilesStack.Outputs.FileSystemArn
      - Sid: InvokeBedrock
        Effect: Allow
        Action:
          - bedrock:InvokeModel
          - bedrock:InvokeModelWithResponseStream
          - bedrock:Converse
          - bedrock:ConverseStream
        Resource: '*'
```

### IAM for orchestrators (invoke agents + S3 Files + EventBridge)
```yaml
Policies:
  - Version: '2012-10-17'
    Statement:
      - Sid: InvokeAgents
        Effect: Allow
        Action: lambda:InvokeFunction
        Resource:
          - !GetAtt SecurityAgentFunction.Arn
          - !Sub '${SecurityAgentFunction.Arn}:*'
          # ... other agents
      - Sid: MountS3Files
        Effect: Allow
        Action:
          - s3files:ClientMount
          - s3files:ClientWrite
          - s3files:ClientRootAccess
        Resource: !GetAtt S3FilesStack.Outputs.FileSystemArn
      - Sid: EmitEvents
        Effect: Allow
        Action: events:PutEvents
        Resource: !Sub 'arn:aws:events:${AWS::Region}:${AWS::AccountId}:event-bus/default'
```

## Stack Outputs

### Demo stack (code-review-demo)
- **ApiEndpoint**: https://aiz3wp9ujc.execute-api.eu-central-1.amazonaws.com/Prod/review
- **WorkspaceBucketName**: code-review-demo-s3filesstack-1a1x-workspacebucket-vfatapceq0ny
- **FileSystemArn**: arn:aws:s3files:eu-central-1:088483494489:file-system/fs-06c8bbbf48185efba
- **AccessPointArn**: arn:aws:s3files:eu-central-1:088483494489:file-system/fs-06c8bbbf48185efba/access-point/fsap-094d8cbddbaffa420

### Production stack (code-review-agents)
- **ApiEndpoint**: https://sk7o5d58r7.execute-api.eu-central-1.amazonaws.com/Prod/review
- **WorkspaceBucketName**: code-review-agents-workspacebucket-ibfofgv8tbrk
- **FileSystemArn**: arn:aws:s3files:eu-central-1:088483494489:file-system/fs-08d625e7c00ef797e
- **AccessPointArn**: (use `aws cloudformation describe-stacks --stack-name code-review-agents` to retrieve)

## Testing

### Demo stack (code-review-demo)
Trigger a review:
```bash
aws events put-events --region eu-central-1 --profile demo --entries '[{"Source":"code-review.api","DetailType":"review.requested","Detail":"{\"repo_url\":\"https://github.com/singledigit/event-driven-agents\"}"}]'
```

Check logs:
```bash
aws logs tail /aws/lambda/code-review-analysis-pipeline --region eu-central-1 --profile demo --since 5m --format short | grep DF1
```

Check artifacts:
```bash
aws s3 ls s3://code-review-demo-s3filesstack-1a1x-workspacebucket-vfatapceq0ny/lambda/event-driven-agents/reviews/ --profile demo --region eu-central-1
```

### Full working stack (code-review-agents)
Trigger a review:
```bash
curl -X POST https://sk7o5d58r7.execute-api.eu-central-1.amazonaws.com/Prod/review \
  -H "Content-Type: application/json" \
  -d '{"repo_url": "https://github.com/singledigit/event-driven-agents"}'
```

Or via EventBridge directly:
```bash
aws events put-events --region eu-central-1 --profile demo --entries '[{"Source":"code-review.api","DetailType":"review.requested","Detail":"{\"repo_url\":\"https://github.com/singledigit/event-driven-agents\"}"}]'
```

Check logs:
```bash
aws logs tail /aws/lambda/code-review-analysis-pipeline --region eu-central-1 --profile demo --since 5m --format short | grep DF1
aws logs tail /aws/lambda/code-review-remediation-pipeline --region eu-central-1 --profile demo --since 15m --format short | grep DF2
```

Check artifacts:
```bash
aws s3 ls s3://code-review-agents-workspacebucket-ibfofgv8tbrk/lambda/event-driven-agents/reviews/ --profile demo --region eu-central-1
aws s3 cp s3://code-review-agents-workspacebucket-ibfofgv8tbrk/lambda/event-driven-agents/reviews/executive_summary.json - --profile demo --region eu-central-1 | python3 -m json.tool
```

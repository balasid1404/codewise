# AWS Setup Guide

## Prerequisites

- AWS CLI configured (`aws configure`)
- Docker installed
- Node.js (for CDK)
- Python 3.11+

## Step 1: Install CDK

```bash
npm install -g aws-cdk
```

## Step 2: Deploy Infrastructure

```bash
cd infra/cdk
pip install -r requirements.txt

# First time only
cdk bootstrap

# Deploy
cdk deploy
```

This creates:
- VPC with public/private subnets
- OpenSearch cluster (2 nodes)
- ECS Fargate service (2 tasks)
- Application Load Balancer
- S3 bucket for codebase

## Step 3: Upload Your Codebase

```bash
# Get bucket name from CDK output
aws s3 sync /path/to/your/codebase s3://fault-loc-codebase-<account-id>/
```

## Step 4: Index the Codebase

```bash
# Get API URL from CDK output
curl -X POST http://<alb-dns>/index \
  -H "Content-Type: application/json" \
  -d '{"s3_uri": "s3://fault-loc-codebase-<account-id>/"}'
```

## Step 5: Test Fault Localization

```bash
# With stack trace
curl -X POST http://<alb-dns>/localize \
  -H "Content-Type: application/json" \
  -d '{
    "error_text": "Traceback (most recent call last):\n  File \"app.py\", line 10, in main\n    process()\nValueError: Invalid input"
  }'

# With screenshot (upload to S3 first)
curl -X POST http://<alb-dns>/localize/image \
  -H "Content-Type: application/json" \
  -d '{"image_path": "s3://fault-loc-codebase-<account-id>/screenshots/error.png"}'
```

## Estimated Costs (Monthly)

| Service | Cost |
|---------|------|
| OpenSearch (2x r6g.large) | ~$300 |
| ECS Fargate (2 tasks) | ~$100 |
| ALB | ~$20 |
| S3 | ~$5 |
| Bedrock (usage) | ~$100-300 |
| **Total** | **~$500-700** |

## Cleanup

```bash
cd infra/cdk
cdk destroy
```

Note: S3 bucket and OpenSearch are set to RETAIN. Delete manually if needed.

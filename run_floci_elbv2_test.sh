#!/bin/bash
# Tests REDACT behind a floci-emulated AWS ALB (ELB v2), closing the local
# portion of ROADMAP item 13 / the "real cloud load balancer" gap ROADMAP
# item 12 has disclosed as out of reach since the multi-node OpenSearch/
# Redis HA work. See docker-compose.yml's own comment on the floci
# service ("cloud-sim" profile) for what floci is and its honest limits.
#
# HONEST UNCERTAINTY, stated up front rather than assumed either way:
# floci's own documentation lists ELB v2 as an "In-process" implementation
# (not "Real Docker" the way MSK/RDS/ElastiCache are), with "ALB, NLB,
# target groups, listeners, routing rules" as its stated features. This
# could mean either (a) a full data-plane emulation that actually
# forwards HTTP requests through the load balancer's DNS name to
# registered targets, or (b) a control-plane-only emulation that lets you
# create/describe/tag these resources via the real AWS API shape without
# actually proxying traffic. This script tests BOTH separately and
# reports which one floci actually provides -- not assumed from the
# service table alone. NOT run in this sandbox (no Docker daemon here) --
# syntax-checked only (bash -n, and every `aws elbv2`/`aws ec2` command
# shape checked against the real, documented AWS CLI parameter names, not
# invented). Needs the same live confirmation pass every other new piece
# of infrastructure in this project has gone through.
set -euo pipefail

echo "=== Part 1: bring up floci and a scaled redact-service ==="
docker compose --profile cloud-sim up -d floci
docker compose up -d --scale redact-service=3 redact-service

echo "Waiting for floci to report healthy on :4566..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:4566/_localstack/health >/dev/null 2>&1 \
       || curl -sf http://localhost:4566/health >/dev/null 2>&1; then
        echo "floci responding."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "floci did not become healthy in time. Check: docker compose logs floci"
        exit 1
    fi
    sleep 2
done

export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1
export AWS_ENDPOINT_URL=http://localhost:4566

echo ""
echo "=== Part 2: control-plane test -- create VPC, subnets, target group, ALB, listener ==="
VPC_ID=$(aws ec2 create-vpc --cidr-block 10.0.0.0/16 --query 'Vpc.VpcId' --output text)
echo "VPC: $VPC_ID"

SUBNET_A=$(aws ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block 10.0.1.0/24 \
    --availability-zone us-east-1a --query 'Subnet.SubnetId' --output text)
SUBNET_B=$(aws ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block 10.0.2.0/24 \
    --availability-zone us-east-1b --query 'Subnet.SubnetId' --output text)
echo "Subnets: $SUBNET_A $SUBNET_B (ALB requires 2+ AZs)"

TG_ARN=$(aws elbv2 create-target-group --name redact-tg \
    --protocol HTTP --port 8080 --vpc-id "$VPC_ID" --target-type ip \
    --health-check-path /health --health-check-protocol HTTP \
    --query 'TargetGroups[0].TargetGroupArn' --output text)
echo "Target group: $TG_ARN"

echo "Registering redact-service replica IPs as targets..."
REPLICA_IPS=$(docker compose ps -q redact-service | xargs -I{} \
    docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' {})
TARGETS_JSON="["
FIRST=true
for ip in $REPLICA_IPS; do
    if [ "$FIRST" = false ]; then TARGETS_JSON="${TARGETS_JSON},"; fi
    TARGETS_JSON="${TARGETS_JSON}{\"Id\":\"${ip}\",\"Port\":8080}"
    FIRST=false
done
TARGETS_JSON="${TARGETS_JSON}]"
echo "Targets: $TARGETS_JSON"
aws elbv2 register-targets --target-group-arn "$TG_ARN" --targets "$TARGETS_JSON"

ALB_ARN=$(aws elbv2 create-load-balancer --name redact-alb \
    --subnets "$SUBNET_A" "$SUBNET_B" --type application \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text)
ALB_DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" \
    --query 'LoadBalancers[0].DNSName' --output text)
echo "ALB: $ALB_ARN"
echo "ALB DNS name: $ALB_DNS"

aws elbv2 create-listener --load-balancer-arn "$ALB_ARN" \
    --protocol HTTP --port 80 \
    --default-actions Type=forward,TargetGroupArn="$TG_ARN" >/dev/null
echo "Listener created (HTTP:80 -> target group)."

echo ""
echo "=== Part 3: health check status (does floci actually run health checks?) ==="
sleep 5
aws elbv2 describe-target-health --target-group-arn "$TG_ARN" \
    --query 'TargetHealthDescriptions[*].[Target.Id,TargetHealth.State]' --output table

echo ""
echo "=== Part 4: data-plane test -- does the ALB's DNS name actually proxy real traffic? ==="
echo "Attempting a real HTTP request through the ALB endpoint..."
if curl -sf -m 10 "http://${ALB_DNS}/health" -H "X-Redact-Api-Key: ${REDACT_API_KEY:-}" ; then
    echo ""
    echo "RESULT: floci's ELB v2 DOES proxy real traffic -- full data-plane fidelity confirmed."
else
    echo ""
    echo "RESULT: request through the ALB DNS name did not succeed. This likely means floci's"
    echo "ELB v2 is control-plane-only (API bookkeeping, no actual request forwarding) -- or it"
    echo "could mean a networking/DNS-resolution issue specific to this environment. Either way,"
    echo "this is real information this script was built to surface, not something to guess at"
    echo "from floci's own docs alone. Report which one this actually was back for BUGS_AND_FIXES.md."
fi

echo ""
echo "=== Cleanup reminder (not run automatically) ==="
echo "aws elbv2 delete-load-balancer --load-balancer-arn $ALB_ARN"
echo "aws elbv2 delete-target-group --target-group-arn $TG_ARN"
echo "aws ec2 delete-subnet --subnet-id $SUBNET_A"
echo "aws ec2 delete-subnet --subnet-id $SUBNET_B"
echo "aws ec2 delete-vpc --vpc-id $VPC_ID"
echo "docker compose --profile cloud-sim down"

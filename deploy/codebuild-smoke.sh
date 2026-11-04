#!/usr/bin/env bash
# CodeBuild (MODE=smoke): install kac and run the end-to-end smoke test with the machine client.
set -euo pipefail

: "${APP_STACK_NAME:=KiroAgentCoreStack}"
REGION="${AWS_REGION:-us-east-1}"

output() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$APP_STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

python3 -m venv /tmp/kac-venv
/tmp/kac-venv/bin/pip install --quiet ./cli
export PATH="/tmp/kac-venv/bin:$PATH"

export KAC_RUNTIME_ARN="$(output RuntimeArn)"
export KAC_QUERY_API_URL="$(output QueryApiUrl)"
export KAC_COGNITO_DOMAIN="$(output CognitoDomain)"
export KAC_CLIENT_ID="$(output MachineClientId)"
export KAC_REGION="$REGION"
POOL_ID="$(output UserPoolId)"
# Keep the secret out of the build log: no `set -x`, and it is only exported to child processes.
KAC_CLIENT_SECRET="$(aws cognito-idp describe-user-pool-client --region "$REGION" --user-pool-id "$POOL_ID" \
  --client-id "$KAC_CLIENT_ID" --query UserPoolClient.ClientSecret --output text)"
export KAC_CLIENT_SECRET
export KAC_CONFIG_DIR=/tmp/kac-config

bash scripts/smoke_test.sh ${SMOKE_MODEL:+--model "$SMOKE_MODEL"}

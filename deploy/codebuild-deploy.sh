#!/usr/bin/env bash
# CodeBuild (MODE=deploy): install CDK dependencies and deploy the application stack.
# Settings arrive as environment variables set by install.sh when it starts the build.
set -euo pipefail

: "${APP_STACK_NAME:=KiroAgentCoreStack}"
: "${KIRO_API_KEY_SECRET_NAME:=prod/kiro/api-key}"
: "${GIT_CREDENTIALS_SECRET_NAME:=NONE}"   # NONE = no Git credentials

echo "==> node $(node --version), docker $(docker --version)"
cd infra
npm ci --no-audit --no-fund

ARGS=(
  -c "kiroApiKeySecretName=${KIRO_API_KEY_SECRET_NAME}"
  -c "gitCredentialsSecretName=${GIT_CREDENTIALS_SECRET_NAME}"
)
[[ -n "${DEFAULT_MODEL:-}" ]] && ARGS+=(-c "defaultModel=${DEFAULT_MODEL}")
[[ -n "${COGNITO_DOMAIN_PREFIX:-}" ]] && ARGS+=(-c "cognitoDomainPrefix=${COGNITO_DOMAIN_PREFIX}")

echo "==> cdk deploy ${APP_STACK_NAME}"
npx cdk deploy "${APP_STACK_NAME}" --require-approval never --progress events "${ARGS[@]}" \
  --outputs-file /tmp/outputs.json
echo "==> outputs"
cat /tmp/outputs.json

#!/usr/bin/env bash
# Pre-deployment checks and guided secret setup for kiro-cli on AgentCore.
#
# Usage: scripts/bootstrap.sh [--region us-east-1] [--api-key-secret NAME] [--git-secret NAME] [--skip-git]
#
# The script only checks things and creates missing secrets. It never changes an existing secret.
# Secret values are read without echo and handed to the AWS CLI through a private (0600) temporary
# file that is deleted as soon as the call returns; they never appear in command-line arguments,
# shell history or the terminal.
set -euo pipefail

REGION="us-east-1"
API_KEY_SECRET="prod/kiro/api-key"
GIT_SECRET="prod/kiro/git-credentials"
SKIP_GIT=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --api-key-secret) API_KEY_SECRET="$2"; shift 2 ;;
    --git-secret) GIT_SECRET="$2"; shift 2 ;;
    --skip-git) SKIP_GIT=true; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

FAILED=0
ok()   { printf '  \033[32m✔\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✘\033[0m %s\n' "$1"; FAILED=1; }
section() { printf '\n\033[1m%s\033[0m\n' "$1"; }

TMPDIR_PRIVATE="$(mktemp -d)"
chmod 700 "$TMPDIR_PRIVATE"
trap 'rm -rf "$TMPDIR_PRIVATE"' EXIT

# Create a secret from a value without putting it on the command line.
create_secret() {
  local name="$1" value="$2" description="$3"
  local input="$TMPDIR_PRIVATE/input.json"
  (umask 077; python3 -c 'import json,sys; print(json.dumps({"Name": sys.argv[1], "Description": sys.argv[2], "SecretString": sys.stdin.read().rstrip("\n")}))' \
    "$name" "$description" <<<"$value" > "$input")
  aws secretsmanager create-secret --region "$REGION" --cli-input-json "file://$input" \
    --query ARN --output text >/dev/null
  rm -f "$input"
}

secret_exists() {
  aws secretsmanager describe-secret --region "$REGION" --secret-id "$1" >/dev/null 2>&1
}

# Same rules as runtime/src/kiro_runtime/secrets.py:parse_git_credentials.
VALIDATE_GIT_JSON='
import json, re, sys
reserved = {"PATH", "HOME", "USER", "SHELL", "PWD", "KIRO_API_KEY", "ARTIFACTS_DIR"}
try:
    d = json.loads(sys.stdin.read())
except ValueError:
    sys.exit("    not valid JSON")
if not isinstance(d, dict) or not d:
    sys.exit("    must be a non-empty JSON object")
for k, v in d.items():
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", k) or k in reserved or k.startswith(("AWS_", "LD_", "KIRO_", "PYTHON")):
        sys.exit("    invalid or reserved key: " + k)
    if not isinstance(v, str):
        sys.exit("    value of " + k + " must be a string")
'

section "1. Tools"
check_tool() {
  local cmd="$1" label="$2"
  if command -v "$cmd" >/dev/null 2>&1; then ok "$label: $("$cmd" --version 2>&1 | head -1)"; else fail "$label not found"; fi
}
check_tool node "Node.js"
if command -v node >/dev/null 2>&1; then
  major="$(node -p 'process.versions.node.split(".")[0]')"
  [[ "$major" -ge 20 ]] || fail "Node.js 20 or newer is required (found $major)"
fi
check_tool aws "AWS CLI"
check_tool python3 "Python"
if command -v python3 >/dev/null 2>&1; then
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null \
    || warn "Python 3.12+ is needed to run the tests and kac (uv can install it: uv python install 3.12)"
fi
command -v uv >/dev/null 2>&1 && ok "uv: $(uv --version)" || warn "uv not found (needed for tests and installing kac)"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  arch="$(docker info --format '{{.Architecture}}' 2>/dev/null || true)"
  if [[ "$arch" == "aarch64" || "$arch" == "arm64" ]]; then
    ok "Docker running (native arm64)"
  else
    warn "Docker is $arch; building the linux/arm64 image needs QEMU/binfmt emulation and is much slower"
  fi
else
  fail "Docker is not running (needed to build the runtime image)"
fi
if [[ -x infra/node_modules/.bin/cdk ]] || command -v cdk >/dev/null 2>&1; then
  ok "AWS CDK CLI available"
else
  warn "CDK CLI not installed yet; run: (cd infra && npm ci)"
fi

section "2. AWS identity"
if ident="$(aws sts get-caller-identity --query '[Account,Arn]' --output text 2>/dev/null)"; then
  ok "credentials: $ident"
  ACCOUNT="$(cut -f1 <<<"$ident")"
else
  fail "no valid AWS credentials"
  ACCOUNT=""
fi
[[ "$REGION" == "us-east-1" ]] && ok "region: $REGION" || warn "region is $REGION; this project is designed for us-east-1"

section "3. CDK bootstrap"
if [[ -n "$ACCOUNT" ]]; then
  if status="$(aws cloudformation describe-stacks --region "$REGION" --stack-name CDKToolkit \
      --query 'Stacks[0].StackStatus' --output text 2>/dev/null)"; then
    ok "CDKToolkit stack: $status"
  else
    fail "account not bootstrapped; run: (cd infra && npx cdk bootstrap aws://$ACCOUNT/$REGION)"
  fi
fi

section "4. Kiro API key secret ($API_KEY_SECRET)"
if [[ -n "$ACCOUNT" ]]; then
  if secret_exists "$API_KEY_SECRET"; then
    ok "exists (value not read)"
  else
    warn "missing. Create an API key in the Kiro portal (Headless mode: https://kiro.dev/docs/cli/headless/)."
    read -r -p "    Create it now? [y/N] " answer
    if [[ "$answer" =~ ^[Yy]$ ]]; then
      read -r -s -p "    Paste the Kiro API key (input hidden): " key; echo
      if [[ -z "$key" ]]; then
        fail "empty value; nothing created"
      else
        [[ "$key" == ksk_* ]] || warn "the value does not start with ksk_; check that it is a Kiro API key"
        create_secret "$API_KEY_SECRET" "$key" "Kiro CLI API key for kiro-cli on AgentCore"
        unset key
        ok "created"
      fi
    else
      fail "required secret missing"
    fi
  fi
fi

section "5. Git credentials secret ($GIT_SECRET, optional)"
if [[ "$SKIP_GIT" == true ]]; then
  warn "skipped; deploy with: npx cdk deploy -c gitCredentialsSecretName=NONE"
elif [[ -n "$ACCOUNT" ]]; then
  if secret_exists "$GIT_SECRET"; then
    ok "exists (value not read)"
  else
    warn "missing. It holds a JSON object of environment variables, e.g. {\"GITHUB_TOKEN\": \"github_pat_...\"}."
    warn "Use a fine-grained token limited to the repositories the agent may touch."
    read -r -p "    Create it now? [y/N] " answer
    if [[ "$answer" =~ ^[Yy]$ ]]; then
      read -r -s -p "    Paste the JSON (input hidden): " creds; echo
      if python3 -c "$VALIDATE_GIT_JSON" <<<"$creds"; then
        create_secret "$GIT_SECRET" "$creds" "Git credentials (environment variables) for kiro-cli on AgentCore"
        ok "created"
      else
        fail "invalid JSON; nothing created"
      fi
      unset creds
    else
      warn "not created; tasks will run without Git credentials (deploy with -c gitCredentialsSecretName=NONE to skip the reference)"
    fi
  fi
fi

section "6. CloudWatch Transaction Search (AgentCore traces)"
if dest="$(aws xray get-trace-segment-destination --region "$REGION" --query '[Destination,Status]' --output text 2>/dev/null)"; then
  if [[ "$dest" == $'CloudWatchLogs\tACTIVE' ]]; then
    ok "enabled"
  else
    warn "not enabled ($dest). Traces will be missing until you enable it:"
    warn "  https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Enable-TransactionSearch.html"
  fi
else
  warn "could not read the trace destination (needs xray:GetTraceSegmentDestination)"
fi

echo
if [[ "$FAILED" -ne 0 ]]; then
  echo "Some checks failed. Fix them and run this script again."
  exit 1
fi
echo "All required checks passed. Next: (cd infra && npx cdk deploy)"

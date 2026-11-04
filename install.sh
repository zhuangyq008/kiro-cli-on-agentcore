#!/usr/bin/env bash
# kiro-cli on AWS AgentCore —— 交互式安装 / 升级向导
#
# 推荐在 AWS CloudShell 中运行（美国东部（弗吉尼亚北部）us-east-1）：
#   ./install.sh
#
# 再次运行即可升级，或在 CloudShell 断开后接着查看进度。
#
# 选项（一般不需要）：
#   --yes            所有确认都按默认值回答（secret 必须已存在）
#   --email EMAIL    登录用户的邮箱（不再询问）
#   --skip-user      不创建登录用户
#   --smoke-only     只重新运行冒烟测试
#   --no-kac         不在本机安装 kac 命令行
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
ROOT="$(pwd)"
VERSION="$(cat VERSION 2>/dev/null || echo dev)"

REGION="us-east-1"
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION" AWS_PAGER=""
APP_STACK="KiroAgentCoreStack"
INSTALLER_STACK="KiroAgentCoreInstaller"
PROJECT="kiro-agentcore-installer"
API_KEY_SECRET="${KIRO_API_KEY_SECRET_NAME:-prod/kiro/api-key}"
GIT_SECRET="${GIT_CREDENTIALS_SECRET_NAME:-prod/kiro/git-credentials}"
CDK_CLI_VERSION="2.1147.0"

ASSUME_YES=false
EMAIL=""
SKIP_USER=false
SMOKE_ONLY=false
INSTALL_KAC=true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) ASSUME_YES=true; shift ;;
    --email) EMAIL="$2"; shift 2 ;;
    --skip-user) SKIP_USER=true; shift ;;
    --smoke-only) SMOKE_ONLY=true; shift ;;
    --no-kac) INSTALL_KAC=false; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（用 --help 查看用法）" >&2; exit 2 ;;
  esac
done

# ------------------------------------------------------------------ 输出与输入
if [[ -t 1 ]]; then
  B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; C=$'\033[36m'; N=$'\033[0m'
else
  B=""; G=""; Y=""; R=""; C=""; N=""
fi
STEP=0
TOTAL_STEPS=8
step()  { STEP=$((STEP + 1)); printf '\n%s[%d/%d] %s%s\n' "$B" "$STEP" "$TOTAL_STEPS" "$1" "$N"; }
info()  { printf '  %s\n' "$1"; }
ok()    { printf '  %s✔%s %s\n' "$G" "$N" "$1"; }
warn()  { printf '  %s!%s %s\n' "$Y" "$N" "$1"; }
die()   { printf '\n  %s✘ %s%s\n' "$R" "$1" "$N" >&2; [[ -n "${2:-}" ]] && printf '    %s\n' "$2" >&2; exit 1; }

# 丢弃等待期间误按的按键，避免它们被下一个问题当成回答。
flush_input() { while read -r -t 0.05 -n 10000 _discard 2>/dev/null; do :; done; }

# confirm "问题" 默认值(Y/N)
confirm() {
  local question="$1" default="${2:-Y}" answer hint
  [[ "$default" == "Y" ]] && hint="[Y/n]" || hint="[y/N]"
  if $ASSUME_YES; then [[ "$default" == "Y" ]]; return; fi
  flush_input
  read -r -p "  ${C}?${N} ${question} ${hint} " answer || answer=""
  answer="${answer:-$default}"
  [[ "$answer" =~ ^[Yy] ]]
}

ask() {  # ask "问题" -> 回显输入
  local answer
  flush_input
  read -r -p "  ${C}?${N} $1 " answer || answer=""
  printf '%s' "$answer"
}

ask_secret() {  # ask_secret "问题" -> 输入不显示在屏幕上
  local answer
  flush_input
  read -r -s -p "  ${C}?${N} $1 " answer || answer=""
  echo >&2
  printf '%s' "$answer"
}

TMP="$(mktemp -d)"
chmod 700 "$TMP"
trap 'rm -rf "$TMP"' EXIT

stack_status() {
  aws cloudformation describe-stacks --stack-name "$1" --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo NONE
}
stack_output() {
  aws cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text
}

# 不经过命令行参数创建 secret：值写入仅本用户可读的临时文件，调用后立即删除。
create_secret() {
  local name="$1" value="$2" description="$3" input="$TMP/secret.json"
  (umask 077; python3 -c 'import json,sys; print(json.dumps({"Name": sys.argv[1], "Description": sys.argv[2], "SecretString": sys.stdin.read().rstrip("\n")}))' \
    "$name" "$description" <<<"$value" >"$input")
  aws secretsmanager create-secret --cli-input-json "file://$input" --query ARN --output text >/dev/null
  rm -f "$input"
}
secret_exists() { aws secretsmanager describe-secret --secret-id "$1" >/dev/null 2>&1; }

# ------------------------------------------------------------------ CodeBuild
PHASE_LABEL() {
  case "$1" in
    SUBMITTED|QUEUED|PROVISIONING) echo "准备构建环境" ;;
    DOWNLOAD_SOURCE) echo "下载源码" ;;
    INSTALL) echo "安装构建工具" ;;
    PRE_BUILD|BUILD) echo "$2" ;;
    POST_BUILD|UPLOAD_ARTIFACTS|FINALIZING) echo "收尾" ;;
    COMPLETED) echo "完成" ;;
    *) echo "$1" ;;
  esac
}

build_logs_tail() {  # build_logs_tail BUILD_ID N
  local stream
  stream="$(aws codebuild batch-get-builds --ids "$1" --query 'builds[0].logs.streamName' --output text 2>/dev/null || true)"
  [[ -z "$stream" || "$stream" == "None" ]] && return 0
  aws logs get-log-events --log-group-name /aws/codebuild/kiro-agentcore-installer --log-stream-name "$stream" \
    --limit "$2" --query 'events[].message' --output text 2>/dev/null | tr '\t' '\n' | sed 's/^/    | /'
}

build_logs_grep() {  # build_logs_grep BUILD_ID PATTERN
  local stream
  stream="$(aws codebuild batch-get-builds --ids "$1" --query 'builds[0].logs.streamName' --output text)"
  aws logs filter-log-events --log-group-name /aws/codebuild/kiro-agentcore-installer --log-stream-names "$stream" \
    --filter-pattern "$2" --query 'events[].message' --output text 2>/dev/null | tr '\t' '\n' | sed '/^$/d'
}

# watch_build BUILD_ID "BUILD 阶段的说明"  -> 0 成功，1 失败
watch_build() {
  local id="$1" label="$2" start now last="" phase status elapsed
  start=$(date +%s)
  while true; do
    read -r phase status < <(aws codebuild batch-get-builds --ids "$id" \
      --query 'builds[0].[currentPhase,buildStatus]' --output text)
    now=$(date +%s); elapsed=$(( (now - start) / 60 ))
    local text; text="$(PHASE_LABEL "$phase" "$label")"
    if [[ "$text" != "$last" ]]; then
      printf '  %s… %s（已用时 %d 分钟）\n' "$(date +%H:%M)" "$text" "$elapsed"
      last="$text"
    fi
    case "$status" in
      SUCCEEDED) return 0 ;;
      FAILED|FAULT|STOPPED|TIMED_OUT) return 1 ;;
    esac
    sleep 15
  done
}

latest_build() {  # 输出 "BUILD_ID STATUS MODE"，没有则为空
  local id
  id="$(aws codebuild list-builds-for-project --project-name "$PROJECT" --sort-order DESCENDING \
    --query 'ids[0]' --output text 2>/dev/null || true)"
  [[ -z "$id" || "$id" == "None" ]] && return 0
  aws codebuild batch-get-builds --ids "$id" \
    --query "builds[0].[id,buildStatus,environment.environmentVariables[?name=='MODE'].value|[0]]" --output text
}

upload_source() {
  local bucket="$1" key="source/$(date -u +%Y%m%dT%H%M%SZ).zip"
  (cd "$ROOT" && zip -qr "$TMP/source.zip" . \
    -x '.git/*' '*/node_modules/*' '*/.venv/*' 'infra/cdk.out/*' '*/__pycache__/*' '*/.pytest_cache/*' \
       '*/.ruff_cache/*' '*.zip' '.DS_Store' '*/.DS_Store')
  aws s3 cp --only-show-errors "$TMP/source.zip" "s3://$bucket/$key"
  printf '%s/%s' "$bucket" "$key"
}

start_build() {  # start_build MODE SOURCE_LOCATION
  local env_json
  env_json="$(python3 -c '
import json, sys
mode, api, git = sys.argv[1:4]
print(json.dumps([
  {"name": "MODE", "value": mode, "type": "PLAINTEXT"},
  {"name": "KIRO_API_KEY_SECRET_NAME", "value": api, "type": "PLAINTEXT"},
  {"name": "GIT_CREDENTIALS_SECRET_NAME", "value": git, "type": "PLAINTEXT"},
]))' "$1" "$API_KEY_SECRET" "${GIT_SECRET_EFFECTIVE:-NONE}")"
  aws codebuild start-build --project-name "$PROJECT" --source-type-override S3 \
    --source-location-override "$2" --environment-variables-override "$env_json" \
    --query 'build.id' --output text
}

show_failure() {
  local id="$1"
  printf '\n  %s构建失败。%s\n' "$R" "$N"
  # 部署类失败最有用的是 CloudFormation 给出的原因，而不是构建日志的最后几行。
  local reasons
  reasons="$(aws cloudformation describe-stack-events --stack-name "$APP_STACK" --max-items 200 \
    --query "StackEvents[?contains(ResourceStatus,'FAILED') && !contains(ResourceStatusReason,'cancelled') && !contains(ResourceStatusReason,'following resource(s) failed')].[LogicalResourceId,ResourceStatusReason]" \
    --output text 2>/dev/null | head -5 || true)"
  if [[ -n "$reasons" ]]; then
    info "CloudFormation 报告的原因："
    sed 's/^/    • /' <<<"$reasons"
  fi
  info "构建日志的最后部分："
  build_logs_tail "$id" 30
  echo
  info "完整日志：CloudWatch 控制台 → 日志组 /aws/codebuild/kiro-agentcore-installer"
  info "修复问题后重新运行 ./install.sh 即可，已经创建的资源会被复用。"
}

# ================================================================== 开始
cat <<EOF

${B}kiro-cli on AWS AgentCore 安装向导${N}  (版本 ${VERSION})

这个向导会在你的 AWS 账号里部署一个远程 Kiro agent 服务。整个过程大约 20–30 分钟，
需要你回答几个问题。期间：
  • 镜像构建和部署在云端（AWS CodeBuild）进行，不占用本机资源；
  • CloudShell 如果断开，云端会继续运行，重新执行 ${B}./install.sh${N} 即可接着查看进度；
  • 等待期间请每隔 15 分钟左右在窗口里按一次回车，避免 CloudShell 因空闲断开。

部署后会持续产生少量费用（以按量计费为主：AgentCore 运行时长、DynamoDB、S3、Lambda 等），
执行任务还会消耗 Kiro credits。
EOF

# ------------------------------------------------------------------ 1. 环境
step "检查运行环境"
for tool in aws python3 zip; do
  command -v "$tool" >/dev/null 2>&1 || die "找不到命令 $tool" "建议在 AWS CloudShell 中运行本向导，它已预装所需工具。"
done
ok "所需工具已就绪"
[[ "${AWS_EXECUTION_ENV:-}" == CloudShell* ]] && ok "运行在 AWS CloudShell 中" || warn "没有检测到 CloudShell，按普通终端继续"

if ! IDENT="$(aws sts get-caller-identity --query '[Account,Arn]' --output text 2>/dev/null)"; then
  die "无法获取 AWS 身份" "请确认已登录 AWS 控制台后再打开 CloudShell；在本地运行时请先配置 AWS 凭证。"
fi
ACCOUNT="$(cut -f1 <<<"$IDENT")"
ARN="$(cut -f2 <<<"$IDENT")"
ok "AWS 账号：${B}${ACCOUNT}${N}"
ok "当前身份：${ARN}"
ok "部署区域：${B}us-east-1（美国东部，弗吉尼亚北部）${N}"
[[ "$ARN" == *":root" ]] && warn "你正在使用根用户。可以继续，但建议日常使用带管理员权限的 IAM 用户或角色。"
info "部署需要管理员级别的权限（会创建 IAM 角色、Cognito、DynamoDB、S3、Lambda 等资源）。"
confirm "确认部署到上面这个账号？" Y || die "已取消。"

if $SMOKE_ONLY; then
  TOTAL_STEPS=$((STEP + 1))
  [[ "$(stack_status "$APP_STACK")" == *COMPLETE ]] || die "还没有部署成功，请先完整运行 ./install.sh。"
  GIT_SECRET_EFFECTIVE="$GIT_SECRET"
fi

if ! $SMOKE_ONLY; then
# ------------------------------------------------------------------ 2. Kiro API key
step "配置 Kiro API key"
if secret_exists "$API_KEY_SECRET"; then
  ok "已存在 secret ${API_KEY_SECRET}，直接使用（没有读取它的值）"
else
  cat <<EOF
  服务需要一个 Kiro API key 来调用 Kiro。获取方法：
    1. 用浏览器打开 https://app.kiro.dev 并登录；
    2. 在账号设置里找到 API keys，新建一个 key；
    3. 复制生成的 key（以 ksk_ 开头）。
  说明：https://kiro.dev/docs/cli/headless/
  key 会保存在 AWS Secrets Manager 的 ${API_KEY_SECRET} 中，只有本服务能读取。
EOF
  $ASSUME_YES && die "secret ${API_KEY_SECRET} 不存在，--yes 模式下无法输入。"
  while true; do
    KEY="$(ask_secret "粘贴 Kiro API key（输入内容不会显示）：")"
    if [[ -z "$KEY" ]]; then warn "没有输入内容，请重试。"; continue; fi
    if [[ "$KEY" != ksk_* ]]; then
      warn "这个值不是以 ksk_ 开头，可能不是 Kiro API key。"
      confirm "仍然使用它吗？" N || continue
    fi
    break
  done
  create_secret "$API_KEY_SECRET" "$KEY" "Kiro CLI API key for kiro-cli on AgentCore"
  unset KEY
  ok "已保存到 Secrets Manager：${API_KEY_SECRET}"
fi

# ------------------------------------------------------------------ 3. Git
step "配置 Git 凭证（可选）"
GIT_SECRET_EFFECTIVE=""
if secret_exists "$GIT_SECRET"; then
  ok "已存在 secret ${GIT_SECRET}，直接使用"
  GIT_SECRET_EFFECTIVE="$GIT_SECRET"
else
  info "如果希望 agent 访问私有 GitHub 仓库、推送代码或创建 PR，需要提供一个 GitHub token。"
  info "只处理公开仓库或不涉及仓库的任务可以跳过，以后重新运行本向导还能再配置。"
  if confirm "现在配置 GitHub token 吗？" N; then
    cat <<EOF
  创建方法（推荐 fine-grained token，只授权需要的仓库）：
    1. 打开 https://github.com/settings/personal-access-tokens/new
    2. Repository access 选择 "Only select repositories"，勾选 agent 需要操作的仓库；
    3. Permissions 中把 Contents 和 Pull requests 设为 Read and write；
    4. 生成后复制 token（以 github_pat_ 开头）。
EOF
    TOKEN="$(ask_secret "粘贴 GitHub token（输入内容不会显示）：")"
    if [[ -n "$TOKEN" ]]; then
      GIT_JSON="$(python3 -c 'import json,sys; print(json.dumps({"GITHUB_TOKEN": sys.stdin.read().strip()}))' <<<"$TOKEN")"
      create_secret "$GIT_SECRET" "$GIT_JSON" "Git credentials (environment variables) for kiro-cli on AgentCore"
      unset TOKEN GIT_JSON
      GIT_SECRET_EFFECTIVE="$GIT_SECRET"
      ok "已保存到 Secrets Manager：${GIT_SECRET}"
    else
      warn "没有输入内容，跳过 Git 凭证。"
    fi
  else
    ok "跳过（任务中无法访问私有仓库）"
  fi
fi

# ------------------------------------------------------------------ 4. CDK bootstrap
step "准备部署工具（CDK bootstrap）"
BOOT_VERSION="$(aws ssm get-parameter --name /cdk-bootstrap/hnb659fds/version --query Parameter.Value --output text 2>/dev/null || echo 0)"
if [[ "$(stack_status CDKToolkit)" == *COMPLETE* && "$BOOT_VERSION" -ge 21 ]]; then
  ok "账号已完成 CDK bootstrap（版本 ${BOOT_VERSION}）"
else
  info "这是账号第一次使用 AWS CDK（或版本过旧），需要创建一组部署用的基础资源（名为 CDKToolkit 的 stack）。"
  confirm "现在创建/更新吗？" Y || die "已取消。CDK bootstrap 是部署的前提。"
  export npm_config_cache="$TMP/npm-cache"   # 不占用 CloudShell 的 1 GB 持久存储
  if command -v cdk >/dev/null 2>&1; then CDK=(cdk); else CDK=(npx --yes "aws-cdk@${CDK_CLI_VERSION}"); fi
  "${CDK[@]}" bootstrap "aws://${ACCOUNT}/${REGION}" --no-notices 2>&1 | tail -3 | sed 's/^/    | /'
  [[ "$(stack_status CDKToolkit)" == *COMPLETE* ]] || die "CDK bootstrap 失败" "请检查当前身份是否有管理员权限。"
  ok "CDK bootstrap 完成"
fi

# ------------------------------------------------------------------ 5. 安装器
step "创建/更新安装器（CodeBuild 项目）"
aws cloudformation deploy --template-file deploy/installer.yaml --stack-name "$INSTALLER_STACK" \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
  --parameter-overrides "ApplicationStackName=${APP_STACK}" >/dev/null \
  || die "安装器创建失败" "在 CloudFormation 控制台查看 ${INSTALLER_STACK} 的事件了解原因。"
SOURCE_BUCKET="$(stack_output "$INSTALLER_STACK" SourceBucketName)"
ok "安装器就绪：CodeBuild 项目 ${PROJECT}"

# ------------------------------------------------------------------ 6. 部署
step "构建镜像并部署服务（约 15–20 分钟）"
BUILD_ID=""
read -r LB_ID LB_STATUS LB_MODE <<<"$(latest_build || true)"
if [[ "${LB_STATUS:-}" == "IN_PROGRESS" && "${LB_MODE:-}" == "deploy" ]]; then
  ok "检测到正在进行的部署，接着查看进度"
  BUILD_ID="$LB_ID"
else
  APP_STATUS="$(stack_status "$APP_STACK")"
  if [[ "$APP_STATUS" == "ROLLBACK_COMPLETE" ]]; then
    warn "上一次首次部署没有成功，留下了一个失败状态的 ${APP_STACK}（里面没有可用资源）。"
    info "需要先删除它才能重新部署。"
    confirm "现在删除并重新部署吗？" Y || die "已取消。"
    aws cloudformation delete-stack --stack-name "$APP_STACK"
    aws cloudformation wait stack-delete-complete --stack-name "$APP_STACK" \
      || die "删除失败" "请在 CloudFormation 控制台查看 ${APP_STACK} 的事件。"
    ok "已删除，继续部署"
    APP_STATUS="NONE"
  fi
  if [[ "$APP_STATUS" == *COMPLETE && "$APP_STATUS" != *ROLLBACK_COMPLETE ]]; then
    info "服务已经部署过，这次会用当前代码升级（未变化的部分不会重建）。"
  fi
  confirm "开始部署吗？" Y || die "已取消。"
  info "正在打包并上传源码…"
  SRC="$(upload_source "$SOURCE_BUCKET")"
  BUILD_ID="$(start_build deploy "$SRC")"
  ok "已在云端开始部署（构建 ID：${BUILD_ID##*:}）"
fi
info "可以离开一会儿；如果 CloudShell 断开，重新运行 ./install.sh 会接着显示进度。"
if ! watch_build "$BUILD_ID" "构建镜像并部署（最耗时的一步）"; then
  show_failure "$BUILD_ID"
  exit 1
fi
ok "服务部署完成"
fi  # end of ! SMOKE_ONLY

POOL_ID="$(stack_output "$APP_STACK" UserPoolId)"

# ------------------------------------------------------------------ 7. 用户
if ! $SMOKE_ONLY; then
step "创建登录用户"
if $SKIP_USER; then
  ok "按参数跳过"
else
  info "使用 kac 命令行提交任务前需要登录。这里为你创建一个用户，临时密码会发到邮箱。"
  while [[ -z "$EMAIL" ]]; do
    EMAIL="$(ask "请输入你的邮箱：")"
    [[ "$EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || { warn "邮箱格式不正确，请重新输入。"; EMAIL=""; }
  done
  EXISTING="$(aws cognito-idp list-users --user-pool-id "$POOL_ID" --filter "email = \"${EMAIL}\"" \
    --query 'Users[0].Username' --output text 2>/dev/null || echo None)"
  if [[ "$EXISTING" != "None" && -n "$EXISTING" ]]; then
    ok "这个邮箱的用户已经存在（用户名 ${EXISTING}），不再重复创建"
    if confirm "需要重新发送临时密码邮件吗？（只对还没有首次登录的用户有效）" N; then
      aws cognito-idp admin-create-user --user-pool-id "$POOL_ID" --username "$EXISTING" \
        --message-action RESEND >/dev/null 2>&1 && ok "已重新发送" || warn "发送失败：该用户可能已完成首次登录"
    fi
  else
    BASE="$(cut -d@ -f1 <<<"$EMAIL" | tr -cd 'A-Za-z0-9._-' | cut -c1-40)"
    USERNAME="${BASE:-user}"
    if aws cognito-idp admin-get-user --user-pool-id "$POOL_ID" --username "$USERNAME" >/dev/null 2>&1; then
      USERNAME="${USERNAME}-$(date +%s | tail -c 5)"
    fi
    aws cognito-idp admin-create-user --user-pool-id "$POOL_ID" --username "$USERNAME" \
      --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true \
      --desired-delivery-mediums EMAIL >/dev/null
    ok "已创建用户 ${B}${USERNAME}${N}（也可以用邮箱登录）"
    info "临时密码已发送到 ${EMAIL}，发件人通常是 no-reply@verificationemail.com，没收到请看垃圾邮件。"
    info "首次登录时会要求设置新密码（至少 12 位，需包含大小写字母、数字和符号）。"
  fi
fi
fi

# ------------------------------------------------------------------ 8. 冒烟测试
step "端到端冒烟测试（约 5–10 分钟，会消耗少量 Kiro credits）"
info "同时提交 3 个测试任务：一个问答、一个小型代码任务、一个故意超时的任务，检查结果是否符合预期。"
read -r LB_ID LB_STATUS LB_MODE <<<"$(latest_build || true)"
if [[ "${LB_STATUS:-}" == "IN_PROGRESS" && "${LB_MODE:-}" == "smoke" ]]; then
  ok "检测到正在进行的冒烟测试，接着查看进度"
  SMOKE_ID="$LB_ID"
else
  SRC="$(upload_source "$(stack_output "$INSTALLER_STACK" SourceBucketName)")"
  SMOKE_ID="$(start_build smoke "$SRC")"
fi
if watch_build "$SMOKE_ID" "运行测试任务"; then
  build_logs_grep "$SMOKE_ID" '?"PASS scenario" ?"PASS listed" ?FAIL ?"SMOKE SUMMARY"' | sed 's/^/    /'
  ok "冒烟测试全部通过"
else
  build_logs_grep "$SMOKE_ID" '?"PASS scenario" ?"PASS listed" ?FAIL ?"SMOKE SUMMARY"' | sed 's/^/    /' || true
  show_failure "$SMOKE_ID"
  info "只重跑测试：./install.sh --smoke-only"
  exit 1
fi

# ------------------------------------------------------------------ kac
if $INSTALL_KAC && ! $SMOKE_ONLY; then
  printf '\n%s安装 kac 命令行%s\n' "$B" "$N"
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    info "安装 uv（Python 工具管理器，装在 ~/.local/bin）…"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null 2>&1 \
      || warn "uv 安装失败，跳过 kac 安装。可以稍后在其他电脑上安装 kac，见 docs/kac.md。"
  fi
  if command -v uv >/dev/null 2>&1; then
    UV_CACHE_DIR="$TMP/uv-cache" uv tool install --quiet --force --python 3.12 "${ROOT}/cli[aws]" >/dev/null 2>&1 \
      && kac configure --from-stack "$APP_STACK" --region "$REGION" >/dev/null \
      && ok "kac 已安装并配置好" \
      || warn "kac 安装失败，可以稍后按 docs/kac.md 手动安装。"
    grep -q '.local/bin' "$HOME/.bashrc" 2>/dev/null || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
  fi
fi

# ------------------------------------------------------------------ 完成
cat <<EOF

${G}${B}安装完成！${N}

${B}接下来：${N}
  1. 在邮箱里找到临时密码，然后在这里登录（按屏幕提示操作）：
       ${C}kac login${N}
  2. 提交第一个任务：
       ${C}kac submit "用 Python 写一个计算斐波那契数列的函数并附上测试" --ac "pytest 全部通过"${N}
  3. 查看进度、等待完成、下载结果：
       ${C}kac status <任务ID>${N}    ${C}kac wait <任务ID>${N}    ${C}kac result <任务ID>${N}

${B}以后：${N}
  • 升级：获取新版代码后重新运行 ./install.sh
  • 使用说明：docs/kac.md；部署与运维：docs/cloudshell-install.md
  • 如果新开的 CloudShell 窗口里找不到 kac，执行：source ~/.bashrc
EOF

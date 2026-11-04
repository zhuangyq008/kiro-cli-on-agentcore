#!/usr/bin/env bash
# End-to-end smoke test against a deployed stack. Consumes Kiro credits.
#
# Prerequisites: kac installed and configured (`kac configure --from-stack KiroAgentCoreStack`),
# and either `kac login` or KAC_CLIENT_ID/KAC_CLIENT_SECRET for a machine client.
# The installer runs this inside CodeBuild (deploy/codebuild-smoke.sh).
#
# The three scenarios run in parallel; each uses its own AgentCore session.
#
# Usage: scripts/smoke_test.sh [--model <model-id>] [--keep]
#   Without --model the tasks use the server's default, i.e. the first preferred model the Kiro API
#   key offers, so the test also works for keys without Claude access.
set -euo pipefail

MODEL=""
KEEP=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    --keep) KEEP=true; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

WORK="$(mktemp -d)"
$KEEP || trap 'rm -rf "$WORK"' EXIT
PASS=0
FAIL=0
pass() { printf 'PASS %s\n' "$1"; PASS=$((PASS + 1)); }
fail() { printf 'FAIL %s\n' "$1"; FAIL=$((FAIL + 1)); }

submit() {
  kac submit "$@" ${MODEL:+--model "$MODEL"} --effort low --json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["task_id"])'
}

# Waits in the background and records the exit code of `kac wait` in $WORK/<task>.code
wait_bg() {
  local task="$1"
  ( code=0; kac wait "$task" --interval 20 >"$WORK/$task.status" 2>"$WORK/$task.progress" || code=$?
    echo "$code" >"$WORK/$task.code" ) &
}

echo "== models"
models_json="$(kac models --json)"
default_model="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("default_model") or "")' <<<"$models_json")"
if [[ -n "$MODEL" ]]; then
  echo "   using --model $MODEL"
elif [[ -n "$default_model" ]]; then
  echo "   server default for this API key: $default_model"
else
  echo "FAIL none of the default models is available to the Kiro API key:" >&2
  kac models --all >&2 || true
  echo "SMOKE SUMMARY passed=0 failed=1"
  exit 1
fi

echo "== submitting"
t1="$(submit "What is 17 multiplied by 3? Write only the number into a file named answer.txt in the directory given by the ARTIFACTS_DIR environment variable." \
  --ac "answer.txt in the artifacts directory contains exactly 51" --max-iterations 2 --timeout 1800)"
echo "   1. question into an artifact (expect SUCCEEDED): $t1"
t2="$(submit "In /workspace/work/calc create a git repository with calc.py defining add(a, b) and a pytest test file for it. Commit both files." \
  --ac "Running 'python -m pytest -q' in /workspace/work/calc passes with at least one test" \
  --ac "The git repository in /workspace/work/calc has a commit containing calc.py" --max-iterations 3 --timeout 3600)"
echo "   2. small code task (expect SUCCEEDED, loop files complete): $t2"
t3="$(submit "Write a detailed 20-section design document about distributed consensus algorithms into the ARTIFACTS_DIR directory." \
  --timeout 60)"
echo "   3. 60 second timeout (expect TIMED_OUT): $t3"

echo "== waiting (usually 3-10 minutes)"
for t in "$t1" "$t2" "$t3"; do wait_bg "$t"; done
wait
code_of() { cat "$WORK/$1.code"; }
for t in "$t1" "$t2" "$t3"; do
  echo "--- $t"; cat "$WORK/$t.status" || true
done

echo "== checks"
pass "models listed (default: ${default_model:-none})"
[[ "$(code_of "$t1")" == 0 ]] && pass "scenario 1 succeeded" || fail "scenario 1 exit code $(code_of "$t1")"
kac result "$t1" --out "$WORK/$t1" >/dev/null
[[ "$(tr -d '[:space:]' < "$WORK/$t1/artifacts/answer.txt" 2>/dev/null)" == "51" ]] \
  && pass "scenario 1 artifact content" || fail "scenario 1 artifact missing or wrong"

[[ "$(code_of "$t2")" == 0 ]] && pass "scenario 2 succeeded" || fail "scenario 2 exit code $(code_of "$t2")"
kac result "$t2" --out "$WORK/$t2" >/dev/null
missing=""
for f in output.md result.json loop/input.json loop/requirements.json loop/requirements.md loop/plan.json loop/plan.md \
         loop/iterations/1/execution.json loop/iterations/1/verify.json; do
  [[ -f "$WORK/$t2/$f" ]] || missing="$missing $f"
done
[[ -z "$missing" ]] && pass "scenario 2 loop files complete" || fail "scenario 2 missing:$missing"

[[ "$(code_of "$t3")" == 2 ]] && pass "scenario 3 timed out" || fail "scenario 3 exit code $(code_of "$t3"), expected 2"

listing="$(kac list --limit 20 --json)"
for t in "$t1" "$t2" "$t3"; do
  grep -q "$t" <<<"$listing" && pass "listed $t" || fail "missing $t in kac list"
done

echo
echo "SMOKE SUMMARY passed=$PASS failed=$FAIL"
$KEEP && echo "downloads kept in $WORK"
[[ "$FAIL" -eq 0 ]]

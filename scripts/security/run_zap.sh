#!/usr/bin/env bash
# Run the Sprint 2 ZAP plan (#159) against ZAP_TARGET with the token of test user A.
#
#   ZAP_HOME=~/Applications/ZAP_2.16.1 ZAP_TARGET=http://127.0.0.1:8080 ZAP_TOKEN=<jwt> scripts/security/run_zap.sh
#
# The token is written only into scripts/security/out/ (git-ignored), never into the plan in git.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
: "${ZAP_HOME:?folder of ZAP (zap.sh)}" "${ZAP_TARGET:?URL to scan}" "${ZAP_TOKEN:?JWT of test user A}"
export ZAP_REPORT_DIR="${ZAP_REPORT_DIR:-$HERE/out}"
export ZAP_VERCEL_BYPASS="${ZAP_VERCEL_BYPASS:-}"
mkdir -p "$ZAP_REPORT_DIR"
python3 - "$HERE/zap-sprint2.yaml" "$ZAP_REPORT_DIR/plan.yaml" <<'PY'
import os, sys
text = open(sys.argv[1]).read()
for name in ("ZAP_TARGET", "ZAP_TOKEN", "ZAP_REPORT_DIR", "ZAP_VERCEL_BYPASS"):
    text = text.replace("${" + name + "}", os.environ[name])
open(sys.argv[2], "w").write(text)
PY
"$ZAP_HOME/zap.sh" -cmd -port "${ZAP_PORT:-8090}" -config start.checkForUpdates=false -autorun "$ZAP_REPORT_DIR/plan.yaml"

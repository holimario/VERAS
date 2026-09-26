#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

patterns=(
  '[[:alnum:]._%+-]+@[[:alnum:].-]+\.[A-Za-z]{2,}'
  '/mlx_devbox/'
  '/home/[A-Za-z0-9._-]+/'
  '/workspace/tmp/'
  'Authorization:[[:space:]]*Bearer[[:space:]]+[A-Za-z0-9._-]+'
  'sk-[A-Za-z0-9_-]{16,}'
  '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
  'codex-native-observer'
  '/benchmark/runtimes/'
  '/benchmark/\.local-deps'
)

failed=0
for pattern in "${patterns[@]}"; do
  if grep -IRnE \
    --exclude-dir=.git \
    --exclude=check_anonymity.sh \
    --exclude=sanitize_trajectories.py \
    "$pattern" "$root"; then
    failed=1
  fi
done

report_root="$root/veras/reports"
for pattern in \
  '"base_url"' \
  '"credential_source"' \
  '"model_provider"' \
  'Reconnecting\.\.\.' \
  '503 Service Unavailable' \
  '业务申请资源不足|平台申请扩容' \
  '<REDACTED_URL>';
do
  if grep -IRnE "$pattern" "$report_root"; then
    failed=1
  fi
done

if find "$root" \
  -path "$root/.git" -prune -o \
  -print \
  | grep -Ei '(^|/)(FAILURE_ANALYSIS|RESEARCH_NOTES)'; then
  failed=1
fi

if [[ "$failed" -ne 0 ]]; then
  echo "anonymity check failed" >&2
  exit 1
fi

echo "anonymity check passed"

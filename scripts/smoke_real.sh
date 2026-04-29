#!/usr/bin/env bash
# smoke_real.sh — real-key end-to-end dry run.
#
# Purpose: verify that your live API keys work by running D1 (hotspots) →
# D2 (write --auto-pick) → D3 (preview). STOPS before publish so we don't
# accidentally ship a test article. After it finishes, inspect the output
# and run `af publish <article_id> ...` manually if you want to actually
# post.
#
# Usage:
#   ./scripts/smoke_real.sh          # real keys (reads backend/.env)
#   MOCK_LLM=true ./scripts/smoke_real.sh   # force mock mode (no keys)
#
# Safe to re-run; appends to ~/.agentflow/memory/events.jsonl each time.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT/backend"

# Preserve an explicit shell override like `MOCK_LLM=true ./scripts/smoke_real.sh`
# even if backend/.env contains a different default.
CLI_MOCK_LLM="${MOCK_LLM-}"
CLI_MOCK_LLM_IS_SET=0
if [ "${CLI_MOCK_LLM+x}" = "x" ]; then
  CLI_MOCK_LLM_IS_SET=1
fi

if [ ! -f .venv/bin/activate ]; then
  echo "ERROR: venv not found at backend/.venv/"
  echo "Bootstrap with:"
  echo "  cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && pip install -e ."
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# Load .env if present (export every key)
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export PYTHONPATH=.
if [ "$CLI_MOCK_LLM_IS_SET" -eq 1 ]; then
  export MOCK_LLM="$CLI_MOCK_LLM"
else
  export MOCK_LLM="${MOCK_LLM:-false}"
fi

echo "== smoke_real.sh  |  MOCK_LLM=$MOCK_LLM =="

# Required-key check (skipped when mocking)
if [ "$MOCK_LLM" != "true" ]; then
  missing=()
  [ -z "${MOONSHOT_API_KEY:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ] \
    && missing+=("MOONSHOT_API_KEY (primary) or ANTHROPIC_API_KEY (fallback)")
  [ -z "${JINA_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ] \
    && missing+=("JINA_API_KEY (primary) or OPENAI_API_KEY (fallback)")
  [ -z "${GHOST_ADMIN_API_KEY:-}" ] \
    && missing+=("GHOST_ADMIN_API_KEY  (for the Ghost preview step)")
  if [ ${#missing[@]} -gt 0 ]; then
    echo
    echo "Missing required env vars in backend/.env:"
    for m in "${missing[@]}"; do echo "  - $m"; done
    echo
    echo "Fill them in, or rerun with MOCK_LLM=true to skip real API calls."
    exit 1
  fi
fi

tmp_scan=$(mktemp -t smoke_scan.XXXXXX.json)
trap 'rm -f "$tmp_scan"' EXIT

echo
echo "[1/4] af hotspots — scan + cluster + viewpoint mining"
af hotspots --json > "$tmp_scan"
hcount=$(python3 -c "import json; print(len(json.load(open('$tmp_scan'))['hotspots']))")
hid=$(python3 -c "import json; print(json.load(open('$tmp_scan'))['hotspots'][0]['id'])")
htopic=$(python3 -c "import json; print(json.load(open('$tmp_scan'))['hotspots'][0]['topic_one_liner'][:60])")
echo "  ok: $hcount hotspots"
echo "  picked: $hid — \"$htopic\""

echo
echo "[2/4] af write --auto-pick — skeleton + full fill"
write_out=$(af write "$hid" --auto-pick --json)
aid=$(echo "$write_out" | python3 -c "import sys,json; print(json.load(sys.stdin)['article_id'])")
words=$(echo "$write_out" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('draft',{}).get('total_word_count','?'))")
echo "  ok: article_id=$aid  (~$words words)"

echo
echo "[3/4] af preview — D3 adapt for ghost_wordpress"
af preview "$aid" --platforms ghost_wordpress --json > /dev/null
preview_file="$HOME/.agentflow/drafts/$aid/platform_versions/ghost_wordpress.md"
if [ ! -f "$preview_file" ]; then
  echo "  FAIL: preview file not written at $preview_file"
  exit 1
fi
plines=$(wc -l < "$preview_file" | tr -d ' ')
echo "  ok: wrote $preview_file ($plines lines)"

echo
echo "[4/4] preview head — first 20 lines of ghost version:"
echo "  ----------------------------------------"
head -n 20 "$preview_file" | sed 's/^/  /'
echo "  ----------------------------------------"

echo
echo "== smoke_real.sh complete =="
echo "  article_id:  $aid"
echo "  draft dir:   ~/.agentflow/drafts/$aid/"
echo "  next:"
echo "    # inspect the draft, then manually publish when you're happy:"
echo "    af publish $aid --platforms ghost_wordpress --force-strip-images --json"
echo "    #                                               ^ strips mock [IMAGE:] placeholders."
echo "    # for a REAL draft, resolve images first via  af image-resolve <id> <ph> <path>"
echo
echo "  logs:"
echo "    tail -n 40 ~/.agentflow/logs/agentflow.log"
echo "    tail -n 10 ~/.agentflow/logs/llm_calls.jsonl   # shows provider used per call"

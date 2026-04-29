#!/usr/bin/env bash
# Build a production-clean agentflow-deploy.tar.gz.
#
# Excluded from the bundle (these would otherwise leak into prod and surface
# fake URLs / fake LLM outputs / test data in TG digests):
#   - tests/                     unit + integration tests
#   - .venv/                     local virtualenv (deploy creates its own)
#   - __pycache__/, .pytest_cache/, .ruff_cache/
#   - *.bak.*                    metadata/draft backup files
#   - .env                       local secrets (we ship .env.template only)
#   - publish_history.jsonl      audit data from local runs
#
# Intentionally INCLUDED:
#   - agentflow/shared/mocks/    LLM/D3 fixtures. These are referenced by
#                                pyproject.toml::package-data and consumed
#                                whenever MOCK_LLM=true. Production defaults
#                                MOCK_LLM=false but operators may flip it
#                                briefly for smoke tests on the VM, and the
#                                fixtures must be on disk for that to work.
#
# Usage:
#   bash scripts/build_deploy_bundle.sh [output_path]
# Default output: ~/Desktop/agentflow-deploy.tar.gz

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT="${1:-$HOME/Desktop/agentflow-deploy.tar.gz}"
STAGE="$(mktemp -d -t agentflow-deploy-XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT

DEST="$STAGE/agentflow-deploy"
mkdir -p "$DEST/backend"

# --- Top-level deploy assets ---
cp "$REPO_ROOT/agentflow-deploy/agentflow-review.service"  "$DEST/" 2>/dev/null || true
cp "$REPO_ROOT/agentflow-deploy/INSTALL_LINUX.md"          "$DEST/"
cp "$REPO_ROOT/agentflow-deploy/SECURITY.md"               "$DEST/"
cp "$REPO_ROOT/agentflow-deploy/deploy.sh"                 "$DEST/"
chmod +x "$DEST/deploy.sh"

# --- config-examples/ at deploy root (REQUIRED — config loaders compute
#     parents[3]/config-examples/, which lands here in the deployed layout) ---
if [ -d "$REPO_ROOT/config-examples" ]; then
  rsync -a "$REPO_ROOT/config-examples/" "$DEST/config-examples/"
else
  echo "FATAL: config-examples/ missing at repo root — config loaders will crash" >&2
  exit 1
fi

# --- Backend code (rsync with explicit excludes) ---
rsync -a \
  --exclude='__pycache__/' \
  --exclude='.pytest_cache/' \
  --exclude='.ruff_cache/' \
  --exclude='.mypy_cache/' \
  --exclude='.venv/' \
  --exclude='.cache/' \
  --exclude='tests/' \
  --exclude='*.egg-info/' \
  --exclude='*.bak.*' \
  --exclude='*.pyc' \
  --exclude='.env' \
  --exclude='.env_copy' \
  --exclude='.env.bak*' \
  --exclude='.env_config*' \
  --exclude='env_config*' \
  --exclude='*env_config copy*' \
  --exclude='secrets/' \
  --exclude='*.key' \
  --exclude='*.pem' \
  --exclude='id_rsa' \
  --exclude='id_rsa.pub' \
  --exclude='test_*.txt' \
  --exclude='test_*.py' \
  --exclude='smoke_*.sh' \
  --exclude='publish_history.jsonl' \
  --exclude='.coverage' \
  --exclude='.DS_Store' \
  "$REPO_ROOT/backend/" "$DEST/backend/"

# --- .env.template at top level (operator copies this to .env) ---
cp "$REPO_ROOT/backend/.env.template" "$DEST/.env.template"

# --- Sanity guards: refuse to ship a bundle that still has test artefacts ---
fail=0
if find "$DEST" -type d -name 'tests' -print -quit | grep -q .; then
  echo "FATAL: tests/ directory survived; check rsync excludes" >&2
  fail=1
fi
# NOTE: mocks/ is now intentionally INCLUDED (see header comment + the
# required-files check below for d2-skeleton.json). Old guard removed.
if find "$DEST" -name '.env' ! -name '.env.template' -print -quit | grep -q .; then
  echo "FATAL: a local .env leaked into the bundle" >&2
  fail=1
fi
# Defensive: catch operator-side key dumps that bypass the .env-named convention
# (e.g. env_config / .env_config / "env_config copy"). v1.0.2 + v1.0.3 bundles
# leaked these because the rsync exclude was exact-match '.env'. This guard is
# rsync-independent — it scans the staged dir post-copy.
if find "$DEST" -type f \( \
    -iname 'env_config*' -o -iname '.env_config*' \
    -o -iname '*.key' -o -iname '*.pem' \
    -o -name 'id_rsa' -o -name 'id_rsa.pub' \
\) -print -quit | grep -q .; then
  echo "FATAL: secret-dump file detected in staged bundle (env_config / .env_config / *.key / *.pem / id_rsa). Review rsync excludes." >&2
  fail=1
fi
# Defensive: scan for any file containing API-key / token assignment patterns.
# Doesn't catch every shape of secret but blocks the most common copy-paste leaks.
if find "$DEST" -type f \( -name '*.env' -o -name '*.env.*' \) ! -name '.env.template' ! -name '*.template' -print 2>/dev/null | grep -q .; then
  echo "FATAL: env-shaped file other than .env.template found in bundle" >&2
  fail=1
fi
# Refuse if .env.template still defaults to MOCK_LLM=true
if grep -E '^[[:space:]]*MOCK_LLM[[:space:]]*=[[:space:]]*true' "$DEST/.env.template" "$DEST/backend/.env.template" 2>/dev/null; then
  echo "FATAL: .env.template still defaults MOCK_LLM=true; production bundles must default false" >&2
  fail=1
fi
# Required runtime files
for required in "config-examples/style_profile.example.yaml" \
                "config-examples/topic_profiles.example.yaml" \
                "backend/prompts/d2_paragraph_filling.md" \
                "backend/pyproject.toml" \
                "backend/requirements.txt" \
                "backend/agentflow/shared/mocks/d2-skeleton.json" \
                "agentflow-review.service" \
                "deploy.sh"; do
  if [ ! -e "$DEST/$required" ]; then
    echo "FATAL: missing required file in bundle: $required" >&2
    fail=1
  fi
done
# Sanity: requirements.txt and pyproject.toml must agree on Pillow
if ! grep -qE '^[Pp]illow' "$DEST/backend/requirements.txt"; then
  echo "FATAL: requirements.txt missing Pillow (pyproject.toml has it; the two must agree)" >&2
  fail=1
fi
# Sanity: .service file must point inside the install prefix (not a hard-coded
# user homedir from the developer's machine)
if grep -qE '/home/[^/]+/agentflow-article-publishing' "$DEST/agentflow-review.service" 2>/dev/null; then
  echo "FATAL: agentflow-review.service still has dev-machine path /home/<user>/agentflow-article-publishing" >&2
  fail=1
fi
[ "$fail" -eq 0 ] || exit 1

# --- Tarball ---
tar -C "$STAGE" -czf "$OUTPUT" agentflow-deploy
size_kb=$(du -k "$OUTPUT" | cut -f1)
file_count=$(tar -tzf "$OUTPUT" | wc -l | tr -d ' ')
echo "✓ wrote $OUTPUT  (${size_kb} KB, ${file_count} entries)"
echo
echo "next:"
echo "  scp $OUTPUT user@vm:/tmp/"
echo "  ssh user@vm 'cd /opt && tar xzf /tmp/$(basename "$OUTPUT")'"
echo "  ssh user@vm 'systemctl restart agentflow-review'"

# AgentFlow Phase Report - 2026-04-27

## Scope

This phase implemented three high-value hardening items for the AgentFlow v1 beta:

1. Improve ChainStream/topic-profile recall quality.
2. Productize Medium manual publishing when the legacy API token is unavailable.
3. Add a weekly learning review loop for long-term operating health.

## Delivered

### 1. ChainStream Hybrid Recall

- `af hotspots --profile <id>` now combines the normal D1 scan with a profile search bundle derived from `topic_profiles.yaml`.
- Scan and search candidates are merged and deduplicated, then reranked by topic fit, freshness, and regex match hints.
- Profile regex matching is now a soft ranking signal in profile mode rather than a hard filter.
- JSON output now includes `recall` and `rerank` metadata, including scan/search/merged/kept counts and a `topic_fit_preview`.
- Existing non-profile `--filter` behavior remains a hard filter for compatibility.

### 2. Medium Manual Publish

- `af publish --platforms medium` no longer fails just because `MEDIUM_INTEGRATION_TOKEN` is missing.
- Without a legacy token, Medium publishing now generates the same artifacts as `af medium-package` and returns `status: manual`.
- The publish result includes `raw_response.manual_required`, `package_path`, `package_json_path`, and related artifact paths.
- Gate D dispatch summaries treat Medium manual publishing as a required handoff rather than a failed platform.
- `MOCK_LLM=true` still preserves the previous mock success path, and legacy token publishing remains supported.

### 3. Weekly Learning Review

- Added `af learning-review --since 7d` for Markdown output.
- Added `af learning-review --since 7d --json` with stable `schema_version: 1`.
- Added `af learning-review --since 7d --post-tg` for posting the review to the configured Telegram review chat.
- The report summarizes constraint suggestions, publish history, platform distribution, memory events, style-learning status, and next-step recommendations.
- Medium `manual` publish status is counted explicitly in the learning review.

## Integration Notes

- Telegram plain-text sending now omits `parse_mode` when callers pass `None`, avoiding Telegram API rejection of `parse_mode: null`.
- The project directory is not currently a git repository, so this report lists changed files and test results instead of git diff metadata.

## Changed Files

- `backend/agentflow/cli/commands.py`
- `backend/agentflow/agent_d1/topic_fit.py`
- `backend/agentflow/agent_d4/main.py`
- `backend/agentflow/agent_d4/publishers/medium.py`
- `backend/agentflow/agent_medium/workflow.py`
- `backend/agentflow/agent_review/render.py`
- `backend/agentflow/agent_review/tg_client.py`
- `backend/agentflow/agent_review/triggers.py`
- `backend/agentflow/cli/learning_review_commands.py`
- `backend/agentflow/shared/learning_review.py`
- `backend/tests/test_v02_workflows.py`

## Verification

- `./.venv/bin/python -m pytest tests/test_v02_workflows.py -q`
- Result: `28 passed`
- IDE lints for edited files: no errors found.

## Remaining Follow-ups

- Run one live Telegram smoke test for `af learning-review --post-tg` after confirming bot credentials.
- Tune topic-fit weights against real ChainStream publishing history after several production scans.
- Consider adding batch apply/dismiss actions for weekly learning suggestions once enough suggestions accumulate.

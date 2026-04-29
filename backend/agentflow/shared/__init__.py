"""Shared foundation: models, LLM client, logging, markdown utils, file readers."""

from agentflow.shared.bootstrap import ensure_user_dirs

# Run once on import — idempotent. Guarantees ~/.agentflow/ tree exists.
ensure_user_dirs()

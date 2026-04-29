"""Agent D0 — style learner.

Ingests past articles (md/docx/txt/url), runs a two-pass LLM pipeline
(per-article analysis → aggregate), and writes ``~/.agentflow/style_profile.yaml``.
"""

from agentflow.agent_d0 import (  # noqa: F401
    aggregator,
    corpus,
    extractor,
    main,
    per_article_analyzer,
)

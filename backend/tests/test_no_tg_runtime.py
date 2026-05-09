"""Phase 3 Wave D: tg_client.py is deleted; triggers.py no longer imports it.

Originally (Phase 2 L-1) this file proved that ``triggers.py`` could load
even when ``tg_client.py`` was absent or its ``requests`` dep was missing.
That guarantee is now permanent: the module is gone, the lazy import
scaffold in ``triggers.py`` was removed in this Wave, so there is nothing
left to gate. The single regression test below verifies that ``triggers``
imports cleanly and that no ``tg_client`` symbol survives at module level
— a guard against accidental re-introduction.
"""

from __future__ import annotations

import unittest


class TriggersHasNoTgClientReferenceTests(unittest.TestCase):
    def test_triggers_imports_without_tg_client(self) -> None:
        from agentflow.agent_review import triggers

        self.assertFalse(
            hasattr(triggers, "tg_client"),
            "triggers.tg_client must not exist after Phase 3 Wave D — re-introducing "
            "it would resurrect the deletion-tolerant scaffold without a Lark twin.",
        )
        self.assertFalse(
            hasattr(triggers, "_TgClientUnavailable"),
            "triggers._TgClientUnavailable sentinel must not exist after Wave D — "
            "the SDK is gone, the deny-stub has no purpose.",
        )

    def test_triggers_emits_lark_review_card_helper_still_present(self) -> None:
        """Sanity: the Lark emit path is the live operator surface now."""
        from agentflow.agent_review import triggers

        self.assertTrue(
            hasattr(triggers, "_emit_lark_review_card"),
            "triggers._emit_lark_review_card is the load-bearing Lark emit; if it "
            "is missing the daemon has no operator surface.",
        )


if __name__ == "__main__":
    unittest.main()

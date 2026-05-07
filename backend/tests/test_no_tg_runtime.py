"""L-1 / IND-4: triggers.py module loads without tg_client SDK present.

Phase 2 acceptance criterion (BLOGFLOW_TG_TO_LARK_PARITY.md §11.5 L-1):
even if tg_client.py is deleted (Phase 3) or its transitive dep `requests`
is unavailable, agentflow.agent_review.triggers must remain importable so
the Lark-only deployment can boot.

Complements test_e2e_lark_pure.py (which proves zero RUNTIME tg_client
calls in the Lark happy path) by proving zero IMPORT-TIME dependency.

Strategy: in-process tests focus on the sentinel CLASS behavior (always
exposed at module level). The actual import-failure simulation runs in a
SUBPROCESS to avoid sys.modules / mock.patch pollution between test
methods inside the main pytest interpreter.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest


class TgClientUnavailableSentinelTests(unittest.TestCase):
    """Direct tests of the sentinel class (always exposed at module level)."""

    def test_sentinel_attribute_access_returns_callable(self) -> None:
        from agentflow.agent_review.triggers import _TgClientUnavailable

        sentinel = _TgClientUnavailable()
        attr = sentinel.send_message
        self.assertTrue(callable(attr))

    def test_sentinel_call_raises_informatively(self) -> None:
        from agentflow.agent_review.triggers import _TgClientUnavailable

        sentinel = _TgClientUnavailable()
        with self.assertRaises(RuntimeError) as ctx:
            sentinel.send_message("chat_x", "body")

        msg = str(ctx.exception)
        self.assertIn("send_message", msg)
        self.assertIn("Lark-only", msg)
        self.assertIn("chat_id", msg)

    def test_sentinel_handles_arbitrary_method_names(self) -> None:
        """Any tg_client method should raise — coverage for delete-tolerance."""
        from agentflow.agent_review.triggers import _TgClientUnavailable

        sentinel = _TgClientUnavailable()
        for method_name in (
            "send_photo",
            "send_document",
            "answer_callback_query",
            "edit_message_reply_markup",
            "send_long_text",
            "get_me",
            "get_updates",
        ):
            with self.assertRaises(RuntimeError) as ctx:
                getattr(sentinel, method_name)("arg")
            self.assertIn(method_name, str(ctx.exception))


class TriggersImportsWithoutTgClientSubprocessTests(unittest.TestCase):
    """Subprocess-isolated test: triggers loads with tg_client unavailable.

    A fresh Python subprocess installs a meta_path finder that blocks
    tg_client BEFORE any agentflow module is imported. Then it imports
    triggers and verifies the sentinel fallback engaged.

    This is the strongest evidence that Phase 3 (deleting tg_client.py)
    won't break Lark deployments at module-load time.
    """

    def test_triggers_loads_in_isolated_subprocess(self) -> None:
        code = textwrap.dedent(
            """
            import sys
            from importlib.abc import MetaPathFinder
            from importlib.machinery import ModuleSpec

            # Install a blocking finder BEFORE any agentflow import.
            # find_spec returns None for non-tg_client modules (let normal
            # finders handle them); for tg_client it raises ImportError.
            class BlockTgClientFinder(MetaPathFinder):
                def find_spec(self, fullname, path, target=None):
                    if fullname.endswith(".tg_client") or fullname == "tg_client":
                        raise ImportError(
                            f"simulated: {fullname} unavailable in Lark-only deploy"
                        )
                    return None

            sys.meta_path.insert(0, BlockTgClientFinder())

            # Now load triggers — should fall back to sentinel
            try:
                from agentflow.agent_review import triggers
            except ImportError as e:
                print(f"FAIL_IMPORT: {e}")
                sys.exit(1)

            # Verify module loaded
            if not hasattr(triggers, "_emit_lark_review_card"):
                print("FAIL_NO_EMIT_HELPER")
                sys.exit(1)

            # Verify sentinel is active
            cls = triggers.tg_client.__class__.__name__
            if cls != "_TgClientUnavailable":
                print(f"FAIL_NO_SENTINEL: got {cls}")
                sys.exit(1)

            # Verify Lark emit path works (returns None when not lark_app_primary)
            result = triggers._emit_lark_review_card(
                "review.gate_a_card",
                article_id=None,
                payload={"short_id": "test01"},
            )
            if result is not None:
                print(f"FAIL_EMIT_RESULT: {result}")
                sys.exit(1)

            # Verify the sentinel raises on call (not silent no-op)
            try:
                triggers.tg_client.send_message("c", "b")
            except RuntimeError as e:
                if "Lark-only" not in str(e):
                    print(f"FAIL_RAISE_MSG: {e}")
                    sys.exit(1)
            else:
                print("FAIL_NO_RAISE")
                sys.exit(1)

            print("OK")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"subprocess failed:\nstdout={result.stdout}\nstderr={result.stderr}",
        )
        self.assertIn("OK", result.stdout)


class TriggersWithTgClientPresentTests(unittest.TestCase):
    """Regression guard: when tg_client IS present (the normal case), the
    module attribute is the real module, not the sentinel. Runs in-process
    because no fancy isolation needed.
    """

    def test_tg_client_is_real_module_in_normal_state(self) -> None:
        from agentflow.agent_review import triggers

        self.assertNotEqual(
            triggers.tg_client.__class__.__name__, "_TgClientUnavailable"
        )
        self.assertTrue(hasattr(triggers.tg_client, "send_message"))


if __name__ == "__main__":
    unittest.main()

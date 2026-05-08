"""L-5 (Phase 2 closure): ``blogflow doctor --fresh`` works in a no-TG deploy.

Phase 2 acceptance criterion (BLOGFLOW_TG_TO_LARK_PARITY.md §11.5 L-5):
on a Lark-only system the operator's first sanity check is ``blogflow doctor``.
It must:

1. Exit 0 with TELEGRAM_BOT_TOKEN unset (no crash, no hang on TG probe).
2. Survive even if ``tg_client.py`` has been deleted (Phase 3 deletion-tolerance) —
   because doctor's ``check_telegram`` is supposed to short-circuit on an
   absent token before the lazy ``tg_client`` import is reached.
3. Never make a live HTTP call to api.telegram.org when the token is unset
   (which is implied by 1+2: no token → no probe → no SDK import → no traffic).

Strategy (mirrors test_no_tg_runtime.py): a fresh Python subprocess installs a
``meta_path`` finder that blocks ``tg_client`` BEFORE click loads. Then it
invokes the click ``doctor`` command in-process via ``CliRunner`` and asserts
exit 0 plus the expected "TELEGRAM_BOT_TOKEN not set" line.

Cache & home isolation: ``AGENTFLOW_HOME`` is pointed at a tmp dir so the
preflight cache (``review/preflight_cache.json``) does not contaminate the
user's real ~/.agentflow.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest


_SUBPROCESS_TEMPLATE = textwrap.dedent(
    """
    import os
    import sys
    from importlib.abc import MetaPathFinder

    class BlockTgClientFinder(MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname.endswith(".tg_client") or fullname == "tg_client":
                raise ImportError(
                    f"simulated: {{fullname}} unavailable in Lark-only deploy"
                )
            return None

    sys.meta_path.insert(0, BlockTgClientFinder())

    from click.testing import CliRunner
    from agentflow.cli.commands import cli

    runner = CliRunner()
    result = runner.invoke(cli, {argv}, catch_exceptions=False)

    print("EXIT_CODE", result.exit_code)
    print("---OUTPUT---")
    print(result.output)
    print("---END---")
    """
)


def _run_doctor_subprocess(
    argv: list[str], extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Spawn an isolated Python process with tg_client blocked, invoke doctor.

    The CLI module auto-loads ``backend/.env`` from the source tree at import
    time (`_load_dotenv_once`), so we cannot rely on ``env.pop`` alone — that
    would let dotenv re-fill the keys with the operator's real values.
    Instead we pre-set them to the empty string in ``os.environ``; dotenv's
    ``override=False`` then leaves them untouched, and preflight's
    ``_env_present`` (which ``.strip()`` + truthy-tests) treats them as absent.
    """
    code = _SUBPROCESS_TEMPLATE.format(argv=repr(argv))
    env = os.environ.copy()
    # Force-clear keys that could let TG probes succeed or skew Lark checks.
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_REVIEW_CHAT_ID",
        "AGENTFLOW_LARK_APP_PRIMARY",
        "AGENTFLOW_AGENT_EVENT_WEBHOOK_URL",
        "AGENTFLOW_AGENT_EVENT_AUTH_HEADER",
        "LARK_WEBHOOK_URL",
        "LARK_APP_ID",
        "LARK_APP_SECRET",
        "LARK_TARGET_CHAT_ID",
    ):
        env[key] = ""
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


class DoctorNoTgTokenTests(unittest.TestCase):
    """TELEGRAM_BOT_TOKEN unset + tg_client blocked → doctor still exits 0."""

    def test_doctor_fresh_exits_zero_with_token_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = _run_doctor_subprocess(
                ["doctor", "--fresh"],
                extra_env={"AGENTFLOW_HOME": tmp},
            )
        combined = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        self.assertIn("EXIT_CODE 0", result.stdout, msg=combined)
        self.assertIn("TELEGRAM_BOT_TOKEN not set", result.stdout, msg=combined)
        # Ensure the blocked finder did not surface as an unhandled error.
        self.assertNotIn("ImportError", result.stdout, msg=combined)
        self.assertNotIn("ModuleNotFoundError", result.stdout, msg=combined)
        self.assertNotIn("Traceback", result.stderr, msg=combined)

    def test_doctor_fresh_lark_primary_exits_zero(self) -> None:
        """Same scenario but with AGENTFLOW_LARK_APP_PRIMARY=true and a webhook
        URL — the Lark-first deployment should be the cleanest happy path."""
        with tempfile.TemporaryDirectory() as tmp:
            result = _run_doctor_subprocess(
                ["doctor", "--fresh"],
                extra_env={
                    "AGENTFLOW_HOME": tmp,
                    "AGENTFLOW_LARK_APP_PRIMARY": "true",
                    "AGENTFLOW_AGENT_EVENT_WEBHOOK_URL": "https://example.test/agent-event",
                    "AGENTFLOW_AGENT_EVENT_AUTH_HEADER": "Bearer test-token",
                },
            )
        combined = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        self.assertIn("EXIT_CODE 0", result.stdout, msg=combined)
        # Telegram check still appears in the matrix but reports "not set".
        self.assertIn("TELEGRAM_BOT_TOKEN not set", result.stdout, msg=combined)
        # Lark App primary check should pass (event_url + auth provided).
        self.assertIn("Lark App primary", result.stdout, msg=combined)
        self.assertIn("review.*_card events enabled", result.stdout, msg=combined)

    def test_doctor_fresh_json_mode_no_tg_traffic(self) -> None:
        """``--json`` exercises the same code path; verify it also avoids
        importing tg_client when the token is unset."""
        with tempfile.TemporaryDirectory() as tmp:
            result = _run_doctor_subprocess(
                ["doctor", "--fresh", "--json"],
                extra_env={"AGENTFLOW_HOME": tmp},
            )
        combined = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        self.assertIn("EXIT_CODE 0", result.stdout, msg=combined)
        # JSON mode must not have crashed on the blocked import either.
        self.assertNotIn("ImportError", result.stdout, msg=combined)
        self.assertNotIn("Traceback", result.stderr, msg=combined)


if __name__ == "__main__":
    unittest.main()

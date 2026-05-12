"""Tests for the Lark operator allowlist (per-action auth).

Covers :func:`agentflow.agent_review.auth.is_authorized_open_id` and the
fail-closed semantics of the new ``lark_operators`` section in ``auth.json``.

Plan ref: docs/BLOGFLOW_TG_TO_LARK_PARITY.md §4 IND-3.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentflow.agent_review import auth as review_auth
from agentflow.shared import bootstrap


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``AGENTFLOW_HOME`` at a tmp dir so each test gets a fresh
    auth.json. Mirrors the patch.object pattern used elsewhere in the suite
    but uses pytest's monkeypatch.setattr per the spec."""
    review_dir = tmp_path / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bootstrap, "AGENTFLOW_HOME", tmp_path)
    return tmp_path


def _write_auth(home_dir: Path, payload: dict) -> Path:
    """Helper: write auth.json directly so tests can exercise the loader
    without depending on the writer code paths."""
    p = home_dir / "review" / "auth.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# is_authorized_open_id
# ---------------------------------------------------------------------------


def test_is_authorized_open_id_with_wildcard_action(home: Path) -> None:
    """Operator with ``["*"]`` is authorized for any action verb."""
    _write_auth(home, {
        "lark_operators": [
            {"open_id": "ou_admin", "name": "Admin", "actions": ["*"]},
        ],
    })
    for action in ("review", "write", "edit", "image", "publish", "system"):
        assert review_auth.is_authorized_open_id("ou_admin", action) is True, (
            f"wildcard grant should authorize {action!r}"
        )


def test_is_authorized_open_id_with_specific_actions(home: Path) -> None:
    """Operator with a closed action list authorizes only those verbs."""
    _write_auth(home, {
        "lark_operators": [
            {
                "open_id": "ou_reviewer",
                "name": "Reviewer",
                "actions": ["review", "edit"],
            },
        ],
    })
    assert review_auth.is_authorized_open_id("ou_reviewer", "review") is True
    assert review_auth.is_authorized_open_id("ou_reviewer", "edit") is True
    assert review_auth.is_authorized_open_id("ou_reviewer", "publish") is False
    assert review_auth.is_authorized_open_id("ou_reviewer", "write") is False


def test_is_authorized_open_id_unknown_open_id(home: Path) -> None:
    """An open_id not in the list is denied even if others are configured."""
    _write_auth(home, {
        "lark_operators": [
            {"open_id": "ou_known", "name": "Known", "actions": ["*"]},
        ],
    })
    assert review_auth.is_authorized_open_id("ou_stranger", "review") is False
    assert review_auth.is_authorized_open_id("ou_stranger", "publish") is False


def test_is_authorized_open_id_empty_lark_operators(home: Path) -> None:
    """Empty ``lark_operators`` array → fail-closed (the IND-3 invariant).

    Phase-2 deployments without an onboarded Lark operator must NOT silently
    allow callbacks just because no one has configured anyone yet.
    """
    _write_auth(home, {"lark_operators": []})
    assert review_auth.is_authorized_open_id("ou_anyone", "review") is False
    assert review_auth.is_authorized_open_id("ou_admin", "publish") is False


def test_is_authorized_open_id_none_input(home: Path) -> None:
    """None / empty open_id → False (no anonymous Lark callbacks)."""
    _write_auth(home, {
        "lark_operators": [
            {"open_id": "ou_admin", "name": "Admin", "actions": ["*"]},
        ],
    })
    assert review_auth.is_authorized_open_id(None, "review") is False
    assert review_auth.is_authorized_open_id("", "review") is False
    assert review_auth.is_authorized_open_id("   ", "review") is False


def test_lark_operators_section_optional(home: Path) -> None:
    """auth.json with only the TG ``authorized_uids`` section loads cleanly;
    every open_id query returns False (fail-closed) without raising."""
    _write_auth(home, {
        "authorized_uids": [
            {"uid": 5306710394, "allowed_actions": ["*"]},
        ],
        # No lark_operators key at all.
    })
    # Loader must not explode.
    assert review_auth._load_lark_operators() == []
    # Every open_id query is denied.
    assert review_auth.is_authorized_open_id("ou_admin", "review") is False
    assert review_auth.is_authorized_open_id("ou_admin", "publish") is False
    # The TG side keeps working (sanity check that we didn't break the loader).
    assert review_auth.is_authorized(5306710394, "publish") is True


# ---------------------------------------------------------------------------
# Round-trip via writer helpers (sanity that the CLI surface produces files
# the reader path accepts).
# ---------------------------------------------------------------------------


def test_lark_operator_add_then_authorize_round_trip(home: Path) -> None:
    review_auth.lark_operator_add(
        "ou_alice", name="Alice", actions=["review", "edit"],
    )
    assert review_auth.is_authorized_open_id("ou_alice", "review") is True
    assert review_auth.is_authorized_open_id("ou_alice", "publish") is False

    # set_actions overwrites; remove drops the entry.
    assert review_auth.lark_operator_set_actions("ou_alice", ["*"]) is True
    assert review_auth.is_authorized_open_id("ou_alice", "publish") is True

    assert review_auth.lark_operator_remove("ou_alice") is True
    assert review_auth.is_authorized_open_id("ou_alice", "review") is False

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.research.research_plan_stop_rule import (
    ResearchPlanStopRule,
    verify_research_plan_stop_rule,
)


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_real_stop_rule_binds_all_failed_or_not_evaluable_sources() -> None:
    contract = verify_research_plan_stop_rule(repo_root=_root())
    assert contract.deployment_decision == "no_go"
    assert contract.reopen_conditions["prominent_manual_user_confirmation_required"] is True
    assert contract.individual_stock_alpha_moratorium["ends_not_before"] == "2028-08-27"
    assert contract.capital_unlock["thirty_percent_controlled_trial_authorized"] is False
    assert contract.capital_unlock["sixty_percent_authorized"] is False
    assert contract.capital_unlock["ninety_percent_authorized"] is False


def test_stop_rule_refuses_automatic_restart() -> None:
    payload = json.loads((_root() / "config/research/research-plan-stop-rule-v1.json").read_text())
    payload["reopen_conditions"]["automatic_restart_forbidden"] = False
    with pytest.raises(ValueError, match="automatic restart"):
        ResearchPlanStopRule.model_validate(payload)

"""The access policy as pure rules (ADR 0028): roles and tiers, data classes against model
retention, connector entitlements, and a broken policy failing at load."""

import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app import config
from app.access import AccessPolicy, Principal, check_references, principal_for_key
from app.auth import ApiKey

BASE: dict[str, Any] = {
    "version": "t1",
    "data_classes": ["public", "internal", "confidential", "restricted"],
    "default_data_class": "internal",
    "group_roles": {
        "eng": "engineer",
        "desk": "trader",
        "compliance": "compliance",
        "everyone": "staff",
    },
    "role_tiers": {
        "engineer": "power",
        "trader": "standard",
        "compliance": "standard",
        "staff": "basic",
    },
    "tier_rank": ["basic", "standard", "power"],
    "models": {
        "vendor/*": {"retention": "30d", "max_data_class": "internal"},
        "vendor/zdr": {"retention": "zero", "max_data_class": "confidential"},
        "preview/*": {"retention": "vendor-default", "max_data_class": "public"},
        "local/*": {"retention": "none", "max_data_class": "restricted"},
    },
    "secondary_use_max_data_class": "internal",
    "connectors": {
        "records": {
            "min_tier": "standard",
            "max_data_class": "restricted",
            "allowed_roles": ["compliance", "trader"],
        },
        "repos": {"min_tier": "power", "max_data_class": "confidential"},
    },
}


def policy(**changes: Any) -> AccessPolicy:
    return AccessPolicy.model_validate({**BASE, **changes})


def who(*groups: str, tier: str | None = None) -> Principal:
    p = policy()
    return Principal("u1", "user", "u1", groups, p.roles(groups), tier or p.tier(groups) or "basic")


# --- who ------------------------------------------------------------------------------


def test_the_highest_tier_among_a_callers_roles_wins() -> None:
    assert policy().tier(("desk", "eng", "everyone")) == "power"
    assert policy().tier(("everyone",)) == "basic"


def test_groups_the_policy_does_not_know_give_no_role_and_no_tier() -> None:
    p = policy()
    assert p.roles(("contractors",)) == ()
    assert p.tier(("contractors",)) is None


def test_an_api_key_is_a_service_principal_whose_team_is_its_group() -> None:
    key = ApiKey(id="k1", name="case-bot", prefix="gw_abc", tier="standard", team="compliance")
    p = principal_for_key(key, policy())
    assert (p.subject, p.kind, p.tier, p.team) == ("key:k1", "service", "standard", "compliance")
    assert p.groups == ("compliance",) and p.roles == ("compliance",)
    assert principal_for_key(ApiKey("k2", "x", "gw_", "basic"), policy()).roles == ()


# --- data handling ----------------------------------------------------------------------


def test_a_model_takes_data_up_to_its_class_and_no_higher() -> None:
    p = policy()
    assert p.check_model("vendor/big", "public").allowed
    assert p.check_model("vendor/big", "internal").allowed
    denied = p.check_model("vendor/big", "confidential")
    assert not denied.allowed and denied.code == "data_class_not_eligible"
    assert "retention 30d" in denied.reason  # the caller is told why


def test_a_models_own_rule_overrides_its_providers() -> None:
    assert policy().check_model("vendor/zdr", "confidential").allowed


def test_a_model_with_no_rule_gets_no_data() -> None:
    denied = policy().check_model("newvendor/shiny", "public")
    assert not denied.allowed and denied.code == "model_unclassified"


def test_a_catch_all_rule_classifies_everything_not_listed() -> None:
    p = policy(models={"*": {"retention": "vendor-default", "max_data_class": "public"}})
    assert p.check_model("newvendor/shiny", "public").allowed
    assert not p.check_model("newvendor/shiny", "internal").allowed


def test_a_chain_is_split_into_eligible_and_excluded_in_order() -> None:
    chain = ["vendor/big", "preview/new", "vendor/zdr", "local/llama"]
    assert policy().eligible(chain, "public") == (chain, [])
    assert policy().eligible(chain, "confidential") == (
        ["vendor/zdr", "local/llama"],
        ["vendor/big", "preview/new"],
    )
    assert policy().eligible(chain, "restricted") == (["local/llama"], chain[:3])


def test_secondary_use_stops_above_its_class() -> None:
    p = policy()
    assert p.allows_secondary_use("internal") and not p.allows_secondary_use("confidential")


# --- connectors -------------------------------------------------------------------------


def test_a_connector_can_be_limited_to_roles_whatever_the_tier() -> None:
    p = policy()
    assert p.check_connector(who("compliance"), "records", "restricted").allowed
    # Engineers have a higher tier, but the connector isn't for their role.
    assert p.check_connector(who("eng"), "records", "internal").code == "role_not_allowed"


def test_a_connector_needs_its_minimum_tier() -> None:
    assert policy().check_connector(who("desk"), "repos", "internal").code == "tier_too_low"
    assert policy().check_connector(who("eng"), "repos", "internal").allowed


def test_a_connector_has_a_data_class_ceiling() -> None:
    assert policy().check_connector(who("eng"), "repos", "restricted").code == (
        "data_class_not_eligible"
    )


def test_a_connector_not_in_the_policy_is_not_onboarded() -> None:
    assert policy().check_connector(who("eng"), "wiki", "public").code == "connector_unknown"


def test_a_tier_outside_the_ranking_can_call_no_connector() -> None:
    assert policy().check_connector(who("eng", tier="chaos"), "repos", "public").code == (
        "tier_unranked"
    )


# --- a broken policy fails at load ----------------------------------------------------------


@pytest.mark.parametrize(
    "broken",
    [
        {"default_data_class": "secret"},  # not a declared class
        {"secondary_use_max_data_class": "secret"},
        {"data_classes": ["public", "public"]},
        {"group_roles": {"eng": "wizard"}},  # a role with no tier
        {"role_tiers": {"engineer": "platinum"}},  # a tier that isn't ranked
        {"models": {"vendor/*": {"retention": "30d", "max_data_class": "secret"}}},
        {"models": {"vendor/*": {"retention": "forever", "max_data_class": "public"}}},
        {"models": {"vendor": {"retention": "30d", "max_data_class": "public"}}},
        {"connectors": {"records": {"min_tier": "platinum", "max_data_class": "public"}}},
        {
            "connectors": {
                "records": {
                    "min_tier": "basic",
                    "max_data_class": "public",
                    "allowed_roles": ["wizard"],
                }
            }
        },
        {"version": ""},
        {"modles": {}},  # a typo must not silently drop the rules
    ],
)
def test_a_broken_policy_is_rejected(broken: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        policy(**broken)


def test_the_policy_must_agree_with_the_rest_of_the_config() -> None:
    p = policy()
    tiers, targets, connectors = (
        {"basic", "standard", "power"},
        {"vendor/big"},
        {"records", "repos"},
    )
    check_references(p, tiers, targets, connectors)
    with pytest.raises(ValueError, match="not a tier in limits.yaml"):
        check_references(p, {"basic", "standard"}, targets, connectors)
    # A model can't be made routable without declaring how its data is handled.
    with pytest.raises(ValueError, match="no data-handling rule for newvendor/shiny"):
        check_references(p, tiers, {"vendor/big", "newvendor/shiny"}, connectors)
    with pytest.raises(ValueError, match="not a registered connector"):
        check_references(p, tiers, targets, {"records"})


@pytest.mark.parametrize("fake", [False, True])
def test_the_shipped_policy_is_valid_for_the_shipped_config(fake: bool) -> None:
    reg = config.load_registry(Path("config"), enable_fake=fake)
    config.check_access(reg, config.load_limits(Path("config")), config.load_access(Path("config")))


def test_a_broken_policy_file_is_refused_on_reload_and_the_old_one_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copytree("config", tmp_path, dirs_exist_ok=True)
    monkeypatch.setattr(config.settings, "config_dir", tmp_path)
    for name in ("registry", "limits", "pricing", "catalog", "guardrails"):
        monkeypatch.setattr(config, name, getattr(config, name))  # restored after the test
    monkeypatch.setattr(config, "access", config.load_access(tmp_path))
    before = config.access.version
    text = (tmp_path / "policy.yaml").read_text()
    (tmp_path / "policy.yaml").write_text(
        text.replace("default_data_class: internal", "default_data_class: secret")
    )
    with pytest.raises(ValueError):
        config.reload_registry()
    assert config.access.version == before and config.access.default_data_class == "internal"
    (tmp_path / "policy.yaml").write_text(text.replace(before, "next"))
    config.reload_registry()
    assert config.access.version == "next"

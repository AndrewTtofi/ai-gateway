"""The access policy: who a caller is, and what they may send where (ADR 0028).

`config/policy.yaml` is the one reviewed place for entitlements and data handling:

    groups → role → tier            what a caller is entitled to
    data class × model retention    which models may see a request's data
    connectors                      which tools a role may call, and with what data

Everything here is a pure function over the parsed file, so every rule is unit-testable
and a decision can always be explained by the policy version that made it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from app.auth import ApiKey

DATA_CLASS_HEADER = "x-gateway-data-class"
_NAME = r"^[a-z0-9][a-z0-9_-]{0,63}$"  # group, role, tier and class names: bounded, loggable


class ModelRule(BaseModel):
    """How one model (or every model of a provider, `provider/*`) may be used."""

    model_config = ConfigDict(extra="forbid")

    # What the provider keeps, from the contract: none (self-hosted), zero (zero data
    # retention agreed), a number of days, or vendor-default (unknown or unbounded).
    retention: str = Field(pattern=r"^(none|zero|[1-9]\d{0,3}d|vendor-default)$")
    max_data_class: str  # the most sensitive class this model may receive


class ConnectorRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_tier: str
    max_data_class: str
    allowed_roles: list[str] = []  # empty = any role with the tier
    # An external tool server (an MCP server, say) that runs the tools itself and asks
    # the gateway whether a user may use them: POST /v1/authorize (ADR 0033).
    external: bool = False
    # The tools an external connector may ask about. Empty = any tool name.
    tools: list[str] = []


class SpendExceptionRule(BaseModel):
    """Who may approve more budget for someone at their cap, and how much (ADR 0032)."""

    model_config = ConfigDict(extra="forbid")

    approver_groups: list[str] = []  # empty: nobody can approve (fail closed)
    max_usd: float = Field(default=100, gt=0)  # the most one request may ask for


class DelegatedDecisionRule(BaseModel):
    """Which services may ask the gateway for a decision about a user (ADR 0033)."""

    model_config = ConfigDict(extra="forbid")

    client_groups: list[str] = []  # empty: nobody may ask (fail closed)


class AccessPolicy(BaseModel):
    """config/policy.yaml. Unknown keys are an error: a typo must not silently drop a rule."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(pattern=r"^[A-Za-z0-9._-]{1,32}$")  # stamped on every decision
    data_classes: list[str] = Field(min_length=1)  # least → most sensitive
    default_data_class: str  # for requests that don't declare one
    group_roles: dict[str, str] = {}  # identity-provider group → role
    role_tiers: dict[str, str] = {}  # role → tier; a caller gets the highest of their roles
    tier_rank: list[str] = Field(min_length=1)  # lowest → highest
    # "provider/model", "provider/*", or "*" for every model not listed. Without "*" an
    # unlisted model gets no data (fail closed).
    models: dict[str, ModelRule] = {}
    # Features that copy prompt content somewhere else (response cache, judge sampling)
    # only run for requests up to this class.
    secondary_use_max_data_class: str
    connectors: dict[str, ConnectorRule] = {}
    spend_exceptions: SpendExceptionRule = SpendExceptionRule()
    delegated_decisions: DelegatedDecisionRule = DelegatedDecisionRule()

    @model_validator(mode="after")
    def _consistent(self) -> AccessPolicy:
        for label, names in (
            ("data_classes", self.data_classes),
            ("tier_rank", self.tier_rank),
            ("roles", [*self.group_roles.values(), *self.role_tiers]),
        ):
            for name in names:
                if not re.fullmatch(_NAME, name):
                    raise ValueError(f"{label}: {name!r} is not a valid name")
        for label, names in (("data_classes", self.data_classes), ("tier_rank", self.tier_rank)):
            if len(names) != len(set(names)):
                raise ValueError(f"{label} must not repeat a name")
        self._known_class("default_data_class", self.default_data_class)
        self._known_class("secondary_use_max_data_class", self.secondary_use_max_data_class)
        for group, role in self.group_roles.items():
            if role not in self.role_tiers:
                raise ValueError(f"group_roles: {group!r} maps to role {role!r} with no tier")
        for role, tier in self.role_tiers.items():
            if tier not in self.tier_rank:
                raise ValueError(f"role_tiers: {role!r} maps to unknown tier {tier!r}")
        for target, rule in self.models.items():
            if target != "*" and not re.fullmatch(r"[^/\s]+/\S+", target):
                raise ValueError(f"models: {target!r} must be provider/model, provider/* or *")
            self._known_class(f"models.{target}.max_data_class", rule.max_data_class)
        for name, c in self.connectors.items():
            if c.min_tier not in self.tier_rank:
                raise ValueError(f"connectors.{name}: unknown tier {c.min_tier!r}")
            self._known_class(f"connectors.{name}.max_data_class", c.max_data_class)
            for role in c.allowed_roles:
                if role not in self.role_tiers:
                    raise ValueError(f"connectors.{name}: unknown role {role!r}")
        return self

    def _known_class(self, where: str, name: str) -> None:
        if name not in self.data_classes:
            raise ValueError(f"{where}: unknown data class {name!r}")

    # --- who ---------------------------------------------------------------------

    def roles(self, groups: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({self.group_roles[g] for g in groups if g in self.group_roles}))

    def tier(self, groups: tuple[str, ...]) -> str | None:
        """The highest tier among the caller's roles; None if no group maps to a role."""
        tiers = [self.role_tiers[r] for r in self.roles(groups)]
        return max(tiers, key=self.tier_rank.index) if tiers else None

    def may_ask_decisions(self, principal: Principal) -> bool:
        """Only a service: a person's token is for calling the gateway as themselves."""
        return principal.kind == "service" and bool(
            set(principal.groups) & set(self.delegated_decisions.client_groups)
        )

    def may_approve_spend(self, principal: Principal) -> bool:
        return bool(set(principal.groups) & set(self.spend_exceptions.approver_groups))

    # --- data handling -------------------------------------------------------------

    def _within(self, data_class: str, max_class: str) -> bool:
        return self.data_classes.index(data_class) <= self.data_classes.index(max_class)

    def model_rule(self, target: str) -> ModelRule | None:
        """The rule for `provider/model`: its own entry, else the provider's `provider/*`,
        else the catch-all `*` if the policy has one."""
        return (
            self.models.get(target)
            or self.models.get(f"{target.partition('/')[0]}/*")
            or self.models.get("*")
        )

    def check_model(self, target: str, data_class: str) -> Decision:
        rule = self.model_rule(target)
        if rule is None:
            # Fail closed: a model nobody has classified gets no data.
            return Decision(False, "model_unclassified", f"{target} has no data-handling rule")
        if not self._within(data_class, rule.max_data_class):
            return Decision(
                False,
                "data_class_not_eligible",
                f"{target} (retention {rule.retention}) is not eligible for '{data_class}' data",
            )
        return Decision(True, "ok", "ok")

    def eligible(self, chain: list[str], data_class: str) -> tuple[list[str], list[str]]:
        """Split a fallback chain into the targets that may see this data, in order, and
        the ones that may not."""
        kept = [t for t in chain if self.check_model(t, data_class).allowed]
        return kept, [t for t in chain if t not in kept]

    def allows_secondary_use(self, data_class: str) -> bool:
        return self._within(data_class, self.secondary_use_max_data_class)

    # --- connectors ----------------------------------------------------------------

    def check_connector(self, principal: Principal, connector: str, data_class: str) -> Decision:
        rule = self.connectors.get(connector)
        if rule is None:
            return Decision(False, "connector_unknown", f"connector '{connector}' is not onboarded")
        if principal.tier not in self.tier_rank:
            return Decision(False, "tier_unranked", f"tier '{principal.tier}' has no rank")
        if self.tier_rank.index(principal.tier) < self.tier_rank.index(rule.min_tier):
            return Decision(
                False,
                "tier_too_low",
                f"tier '{principal.tier}' is below '{rule.min_tier}' for {connector}",
            )
        if rule.allowed_roles and not set(principal.roles) & set(rule.allowed_roles):
            return Decision(False, "role_not_allowed", f"{connector} needs a role it allows")
        if not self._within(data_class, rule.max_data_class):
            return Decision(
                False,
                "data_class_not_eligible",
                f"{connector} is not eligible for '{data_class}' data",
            )
        return Decision(True, "ok", "ok")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str  # a fixed name, safe as a log field
    reason: str  # for the caller and the audit record


@dataclass(frozen=True)
class Principal:
    """Whoever is calling, whatever credential they used. Policy and audit only ever see
    this, so adding a kind of credential doesn't touch the rules."""

    subject: str  # stable id: "key:<id>" for an API key
    kind: Literal["service", "user"]
    name: str  # for humans reading the audit log
    groups: tuple[str, ...]
    roles: tuple[str, ...]
    tier: str
    team: str | None = None


def principal_for_key(key: ApiKey, policy: AccessPolicy) -> Principal:
    """The caller as policy and audit see them.

    An API key is a service identity: an operator assigned its tier directly, and its team
    counts as its group, so a team's keys get that team's role. A user comes from the
    identity provider with the directory's groups."""
    if key.kind == "user":
        groups = key.groups
    else:
        groups = (key.team,) if key.team else ()
    return Principal(
        subject=f"{'user' if key.kind == 'user' else 'key'}:{key.id}",
        kind=key.kind,
        name=key.name,
        groups=groups,
        roles=policy.roles(groups),
        tier=key.tier,
        team=key.team,
    )


def check_references(
    policy: AccessPolicy, tiers: set[str], targets: set[str], connectors: set[str]
) -> None:
    """Fail at load, not at request time, when the policy and the rest of the config
    disagree: a tier limits.yaml doesn't have, a routable model with no data-handling
    rule, or a rule for a connector that doesn't exist."""
    for tier in policy.tier_rank:
        if tier not in tiers:
            raise ValueError(f"policy tier_rank: {tier!r} is not a tier in limits.yaml")
    unclassified = sorted(t for t in targets if policy.model_rule(t) is None)
    if unclassified:
        raise ValueError(
            "policy models: no data-handling rule for "
            + ", ".join(unclassified)
            + " (add the model, or its provider as provider/*)"
        )
    for name, rule in policy.connectors.items():
        if rule.external and name in connectors:
            raise ValueError(f"policy connectors: {name!r} is external but also built in")
        if not rule.external and name not in connectors:
            raise ValueError(f"policy connectors: {name!r} is not a registered connector")
        if rule.tools and not rule.external:
            raise ValueError(f"policy connectors: {name!r} lists tools but is not external")

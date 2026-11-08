"""Trusted launch capabilities only narrow existing roles."""

import pytest

from agent_workbook import coordination as c
from agent_workbook.store import Workbook


@pytest.mark.parametrize("role", c.PERMISSIONS)
def test_default_roles_keep_all_existing_grants(role):
    principal = c.Principal("agent", role)
    for capability, action in c.CAPABILITY_ACTIONS.items():
        if action in c.PERMISSIONS[role]:
            c.authorize_capability(principal, capability)
        else:
            with pytest.raises(PermissionError):
                c.authorize_capability(principal, capability)


def test_restrictions_are_immutable_and_do_not_grant_nested_entrypoints():
    configured = ["task:run"]
    principal = c.Principal("runner", capabilities=configured)
    configured.append("artifact:delete")
    assert principal.capabilities == frozenset({"task:run"})
    c.authorize(principal, "task:run")
    c.authorize(principal, "write")  # Internal task result materialization.
    for action in ("read", "artifact:write", "artifact:delete", "workers", "export"):
        with pytest.raises(PermissionError):
            c.authorize_capability(principal, action)
    with c.acting_as(principal):
        assert c.current_principal() is principal
    assert c.current_principal().capabilities is None


@pytest.mark.parametrize(
    ("role", "capabilities"),
    [
        ("reader", ["task:run"]),
        ("writer", ["approve"]),
        ("approver", ["claims"]),
        ("writer", ["unknown"]),
        ("writer", "read"),
        ("writer", [True]),
    ],
)
def test_invalid_or_escalating_grants_are_rejected(role, capabilities):
    with pytest.raises(ValueError, match="subset"):
        c.Principal("agent", role, capabilities)


def test_empty_grants_deny_every_entrypoint_and_approval():
    principal = c.Principal("agent", "approver", [])
    for capability in c.CAPABILITY_ACTIONS:
        with pytest.raises(PermissionError):
            c.authorize_capability(principal, capability)
    with pytest.raises(PermissionError):
        c.authorize(principal, "approve")


def test_claim_mutations_enforce_capability_at_module_boundary(tmp_path):
    book = Workbook(tmp_path)
    owner = c.Principal("same-agent", capabilities=["claims"])
    restricted = c.Principal("same-agent", capabilities=["task:run"])
    ticket = c.claim(book, owner, "task:example")
    for function, arguments in (
        (c.claim, ("task:other",)),
        (c.renew, (ticket,)),
        (c.release, (ticket,)),
    ):
        with pytest.raises(PermissionError, match="claims"):
            function(book, restricted, *arguments)
    assert c.owner(book, "task:example")["active"]
    assert c.release(book, owner, ticket)["released"]

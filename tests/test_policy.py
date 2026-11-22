import pytest
from pydantic import ValidationError

from managed_agents.policy import (
    AccessDenied,
    Agent,
    Connector,
    Principal,
    Registry,
    authorize_agent,
    authorize_connector,
)


@pytest.fixture
def registry():
    return Registry(
        enabled=True,
        agents=(Agent(id="research", enabled=True, allowed_roles={"research.read"},
                      connectors={"docs"}, data_class="internal"),),
        connectors=(Connector(id="docs", enabled=True, kind="storage",
                              bucket="documents", prefix="approved/"),),
    )


def test_authorized_role_can_invoke(registry):
    agent = authorize_agent(registry, Principal(subject="user", roles={"research.read"}),
                            "research")
    assert authorize_connector(registry, agent, "docs").bucket == "documents"


@pytest.mark.parametrize("roles,agent_id", [(set(), "research"),
                                            ({"other"}, "research"),
                                            ({"research.read"}, "unknown")])
def test_missing_grants_and_unknown_agents_fail_closed(registry, roles, agent_id):
    with pytest.raises(AccessDenied):
        authorize_agent(registry, Principal(subject="user", roles=roles), agent_id)


def test_global_kill_switch(registry):
    with pytest.raises(AccessDenied):
        authorize_agent(registry.model_copy(update={"enabled": False}),
                        Principal(subject="user", roles={"research.read"}), "research")


def test_agent_kill_switch(registry):
    disabled = registry.agents[0].model_copy(update={"enabled": False})
    with pytest.raises(AccessDenied):
        authorize_agent(registry.model_copy(update={"agents": (disabled,)}),
                        Principal(subject="user", roles={"research.read"}), "research")


def test_connector_kill_switch(registry):
    disabled = registry.connectors[0].model_copy(update={"enabled": False})
    with pytest.raises(AccessDenied):
        authorize_connector(registry.model_copy(update={"connectors": (disabled,)}),
                            registry.agents[0], "docs")


def test_connector_grant_required(registry):
    with pytest.raises(AccessDenied):
        authorize_connector(registry, registry.agents[0].model_copy(update={"connectors": set()}),
                            "docs")


def test_data_class_enforced(registry):
    with pytest.raises(AccessDenied):
        authorize_connector(registry, registry.agents[0].model_copy(update={"data_class": "public"}),
                            "docs")


def test_registry_rejects_unknown_connectors():
    with pytest.raises(ValidationError):
        Registry(agents=(Agent(id="research", connectors={"missing"}),))


def test_registry_rejects_duplicate_ids(registry):
    with pytest.raises(ValidationError):
        Registry(agents=registry.agents * 2, connectors=registry.connectors)
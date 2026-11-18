from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Agent(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,62}$")
    enabled: bool = False
    engine_resource: str = ""
    allowed_roles: frozenset[str] = frozenset()
    connectors: frozenset[str] = frozenset()
    model_aliases: frozenset[str] = frozenset()
    data_class: Literal["public", "internal", "confidential"] = "internal"
    max_steps: int = Field(default=8, ge=1, le=50)


class Connector(StrictModel):
    id: str
    enabled: bool = False
    kind: Literal["storage"]
    bucket: str
    prefix: str = Field(min_length=1)
    allowed_roles: frozenset[str] = frozenset()
    data_class: Literal["public", "internal", "confidential"] = "internal"


class ModelAlias(StrictModel):
    id: str
    model: str = Field(pattern=r"^[a-zA-Z0-9._-]+$")
    enabled: bool = False
    data_classes: frozenset[Literal["public", "internal", "confidential"]] = frozenset()
    max_output_tokens: int = Field(default=1024, ge=1, le=8192)


class Registry(StrictModel):
    version: Literal[1] = 1
    enabled: bool = False
    agents: tuple[Agent, ...] = ()
    connectors: tuple[Connector, ...] = ()
    models: tuple[ModelAlias, ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> "Registry":
        agent_ids = [agent.id for agent in self.agents]
        connector_ids = [connector.id for connector in self.connectors]
        model_ids = [model.id for model in self.models]
        if len(set(agent_ids)) != len(agent_ids):
            raise ValueError("Duplicate agent IDs")
        if len(set(connector_ids)) != len(connector_ids):
            raise ValueError("Duplicate connector IDs")
        if len(set(model_ids)) != len(model_ids):
            raise ValueError("Duplicate model IDs")
        for agent in self.agents:
            if not agent.connectors.issubset(connector_ids):
                raise ValueError(f"Unknown connector reference for {agent.id}")
            if not agent.model_aliases.issubset(model_ids):
                raise ValueError(f"Unknown model reference for {agent.id}")
        return self

    @classmethod
    def load(cls, path: Path) -> "Registry":
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class Principal(StrictModel):
    subject: str = Field(min_length=1)
    roles: frozenset[str] = frozenset()


class AccessDenied(Exception):
    pass


def authorize_agent(registry: Registry, principal: Principal, agent_id: str) -> Agent:
    agent = next((item for item in registry.agents if item.id == agent_id), None)
    if not registry.enabled or agent is None or not agent.enabled:
        raise AccessDenied("Agent unavailable")
    if not principal.roles.intersection(agent.allowed_roles):
        raise AccessDenied("Agent access denied")
    return agent


def authorize_connector(registry: Registry, agent: Agent, connector_id: str) -> Connector:
    connector = next((item for item in registry.connectors if item.id == connector_id), None)
    if not registry.enabled or not agent.enabled or connector_id not in agent.connectors:
        raise AccessDenied("Connector access denied")
    if connector is None or not connector.enabled:
        raise AccessDenied("Connector unavailable")
    classification = {"public": 0, "internal": 1, "confidential": 2}
    if classification[connector.data_class] > classification[agent.data_class]:
        raise AccessDenied("Data classification exceeds agent clearance")
    return connector
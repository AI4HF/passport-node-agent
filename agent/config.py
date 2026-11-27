"""
Configuration for the node agent.

One agent runs per organization, so the site-wide settings - which Passport, which Keycloak, which
study, which organization - are shared by all three modules. What is *not* shared is the identity:
each module authenticates as its own Keycloak service account so the Passport's audit log book can
tell "the dataset sync wrote this" from "the monitoring import wrote that".
"""

import os
from dataclasses import dataclass
from typing import Optional, Tuple

MODULES = ("dataset-sync", "metadata-publisher", "monitoring-import")

# Each module's client id and secret are read from its own pair of variables, so a site can issue
# three separate service accounts without the modules sharing a credential.
_CLIENT_ENV = {
    "dataset-sync": ("DATASET_SYNC_CLIENT_ID", "DATASET_SYNC_CLIENT_SECRET"),
    "metadata-publisher": ("METADATA_PUBLISHER_CLIENT_ID", "METADATA_PUBLISHER_CLIENT_SECRET"),
    "monitoring-import": ("MONITORING_IMPORT_CLIENT_ID", "MONITORING_IMPORT_CLIENT_SECRET"),
}


class ConfigError(RuntimeError):
    """Raised when a required setting is absent, so the agent fails at startup rather than midway."""


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"{name} is required but not set")
    return value


@dataclass(frozen=True)
class KeycloakConfig:
    """Where to get a token, and which service account to get it as."""

    server_url: str
    realm: str
    client_id: str
    client_secret: str

    @property
    def token_url(self) -> str:
        return f"{self.server_url.rstrip('/')}/realms/{self.realm}/protocol/openid-connect/token"


@dataclass(frozen=True)
class AgentConfig:
    """Everything one module of the agent needs to run."""

    module: str
    passport_url: str
    study_id: str
    organization_id: str
    keycloak: KeycloakConfig

    # dataset-sync
    feast_url: Optional[str] = None
    dataset_id: Optional[str] = None
    experiment_id: Optional[str] = None
    quality_criteria_ids: Tuple[str, ...] = ()
    definitions_dir: Optional[str] = None

    # metadata-publisher
    fdp_url: Optional[str] = None
    fdp_catalog_id: Optional[str] = None
    fdp_token: Optional[str] = None
    fdp_email: Optional[str] = None
    fdp_password: Optional[str] = None

    # monitoring-import
    elasticsearch_url: Optional[str] = None
    elasticsearch_index: Optional[str] = None
    elasticsearch_basic_auth: Optional[str] = None

    request_timeout: int = 60


def load(module: str) -> AgentConfig:
    """
    Build the configuration for one module from the environment.

    :param module: one of MODULES
    :return: the module's configuration
    :raises ConfigError: when the module is unknown or a required setting is missing
    """
    if module not in MODULES:
        raise ConfigError(f"unknown module {module!r}; expected one of {', '.join(MODULES)}")

    client_id_var, client_secret_var = _CLIENT_ENV[module]

    return AgentConfig(
        module=module,
        passport_url=_require("PASSPORT_SERVER_URL").rstrip("/"),
        study_id=_require("STUDY_ID"),
        organization_id=_require("ORGANIZATION_ID"),
        keycloak=KeycloakConfig(
            server_url=_require("KEYCLOAK_SERVER_URL"),
            realm=os.getenv("KEYCLOAK_REALM", "AI4HF-Authorization"),
            client_id=_require(client_id_var),
            client_secret=_require(client_secret_var),
        ),
        feast_url=(os.getenv("FEAST_URL") or "").rstrip("/") or None,
        dataset_id=os.getenv("DATASET_ID"),
        experiment_id=os.getenv("EXPERIMENT_ID"),
        quality_criteria_ids=tuple(
            c.strip() for c in (os.getenv("QUALITY_CRITERIA_IDS") or "").split(",") if c.strip()
        ),
        definitions_dir=os.getenv("DEFINITIONS_DIR"),
        fdp_url=(os.getenv("FDP_URL") or "").rstrip("/") or None,
        fdp_catalog_id=os.getenv("FDP_CATALOG_ID"),
        fdp_token=os.getenv("FDP_TOKEN"),
        fdp_email=os.getenv("FDP_EMAIL"),
        fdp_password=os.getenv("FDP_PASSWORD"),
        elasticsearch_url=(os.getenv("ELASTICSEARCH_URL") or "").rstrip("/") or None,
        elasticsearch_index=os.getenv("ELASTICSEARCH_INDEX", "evaluation-measures-*"),
        elasticsearch_basic_auth=os.getenv("ELASTICSEARCH_BASIC_AUTH"),
        request_timeout=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "60")),
    )


def require_for_dataset_sync(config: AgentConfig) -> None:
    """Check the settings the dataset-sync module needs beyond the shared ones."""
    missing = [
        name
        for name, value in (
            ("FEAST_URL", config.feast_url),
            ("DATASET_ID", config.dataset_id),
            ("EXPERIMENT_ID", config.experiment_id),
        )
        if not value
    ]
    if missing:
        raise ConfigError(f"dataset-sync requires {', '.join(missing)}")


def require_for_metadata_publisher(config: AgentConfig) -> None:
    """Check the settings the metadata-publisher module needs beyond the shared ones."""
    if not config.fdp_url:
        raise ConfigError("metadata-publisher requires FDP_URL")
    if not config.fdp_catalog_id:
        raise ConfigError("metadata-publisher requires FDP_CATALOG_ID: the catalog the datasets are listed in")
    if not config.fdp_token and not (config.fdp_email and config.fdp_password):
        raise ConfigError("metadata-publisher requires FDP_EMAIL and FDP_PASSWORD (or FDP_TOKEN)")


def require_for_monitoring_import(config: AgentConfig) -> None:
    """Check the settings the monitoring-import module needs beyond the shared ones."""
    if not config.elasticsearch_url:
        raise ConfigError("monitoring-import requires ELASTICSEARCH_URL")

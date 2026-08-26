"""HelloDJ config-renderer component.

Renders the complete Lavalink ``application.yml`` from AWS Secrets Manager
(credentials) and DynamoDB (non-secret config), replacing the legacy
SQLite-backed renderer. Designed to run as an init container / pre-deploy Job.

Requirements: 6.1, 7.3, 15.1
"""

from __future__ import annotations

from .config_source import DynamoConfigSource
from .model import LavalinkCredentials, LavalinkSettings
from .renderer import YOUTUBE_CLIENTS, build_config, render_yaml
from .secrets_source import SecretsManagerCredentialSource

__all__ = [
    "LavalinkCredentials",
    "LavalinkSettings",
    "SecretsManagerCredentialSource",
    "DynamoConfigSource",
    "build_config",
    "render_yaml",
    "YOUTUBE_CLIENTS",
]

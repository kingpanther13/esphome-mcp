"""Constants for the ESPHome MCP custom component."""

DOMAIN = "esphome_mcp"
VERSION = "0.1.5"

DEFAULT_SERVER_PORT = 9590
DEFAULT_BIND_HOST = "0.0.0.0"
BIND_HOST_ALL = "0.0.0.0"
BIND_HOST_LOOPBACK = "127.0.0.1"

# Installed at runtime when the in-process server entry is enabled. Keeping this
# out of manifest.json lets HAOS boot with the baked entry disabled, matching
# ha-mcp's embedded server pattern. FastMCP is process-global inside HA Core, so
# this pin must match the stable ha-mcp release used by ha_mcp_tools.
# renovate: datasource=github-releases depName=homeassistant-ai/ha-mcp
HA_MCP_COMPAT_RELEASE = "v7.12.2"
DEFAULT_PIP_SPEC = "fastmcp==3.4.3"

SERVER_CONFIG_SUBDIR = ".esphome_mcp"

OPT_SERVER_PORT = "server_port"
OPT_BIND_HOST = "bind_host"
OPT_WEBHOOK_AUTH = "webhook_auth"
OPT_EXTERNAL_URL = "external_url"
OPT_WEBHOOK_ID_OVERRIDE = "webhook_id_override"
OPT_SECRET_PATH_OVERRIDE = "secret_path_override"
OPT_REGENERATE_SECRETS = "regenerate_secrets"
OPT_ENABLE_WEBHOOK = "enable_webhook"
OPT_ENABLE_PERSISTENT_NOTIFICATION = "enable_persistent_notification"

DATA_WEBHOOK_ID = "webhook_id"
DATA_SECRET_PATH = "secret_path"
DATA_MANAGER = "manager"
DATA_WEBHOOK = "webhook"
DATA_BRINGUP_TASK = "bringup_task"
DATA_LAST_OPTIONS = "last_options"
DATA_LAST_PIP_SPEC = "last_pip_spec"

WEBHOOK_AUTH_NONE = "none"
WEBHOOK_AUTH_HA = "ha_auth"

OAUTH_BASE = "/api/esphome_mcp/oauth"

ISSUE_PACKAGE_FAILED = "server_package_import_failed"
ISSUE_RESTART_REQUIRED = "server_restart_required"
ISSUE_START_FAILED = "server_start_failed"

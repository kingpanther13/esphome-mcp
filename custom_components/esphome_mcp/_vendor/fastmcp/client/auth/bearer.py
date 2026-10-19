import httpx2
from pydantic import SecretStr

from custom_components.esphome_mcp._vendor.fastmcp.utilities.logging import get_logger

__all__ = ["BearerAuth"]

logger = get_logger(__name__.removeprefix("custom_components.esphome_mcp._vendor."))


class BearerAuth(httpx2.Auth):
    def __init__(self, token: str):
        self.token = SecretStr(token)

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self.token.get_secret_value()}"
        yield request

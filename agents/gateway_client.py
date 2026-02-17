"""AgentCore Gateway MCP client — fetches tools via IAM-authenticated Gateway.

Architecture:
    Agent → MCPClient → AgentCore Gateway (IAM SigV4) → Lambda functions

All tool invocations go through the Gateway. Agents never call AWS
services directly via boto3 — the Lambda behind the Gateway does that.
"""
import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)


class GatewayToolProvider:
    """Connects to AgentCore Gateway and provides MCP tools to agents.

    The Gateway exposes Lambda functions as MCP-compatible tools.
    Authentication uses IAM role credentials from the execution environment
    (Lambda execution role in production, local AWS profile in dev).

    Usage:
        provider = GatewayToolProvider()
        tools = provider.start()
        # ... use tools with Strands Agent ...
        provider.stop()

    Or as a context manager:
        with GatewayToolProvider() as tools:
            agent = Agent(tools=tools, ...)
    """

    def __init__(
        self,
        endpoint_url: Optional[str] = None,
        region: Optional[str] = None,
    ):
        """Initialize the Gateway tool provider.

        Args:
            endpoint_url: Gateway MCP endpoint URL. Falls back to
                          GATEWAY_ENDPOINT env var.
            region: AWS region for SigV4 signing. Falls back to
                    GATEWAY_REGION or AWS_DEFAULT_REGION env var.
        """
        self.endpoint_url = endpoint_url or os.environ.get("GATEWAY_ENDPOINT", "")
        self.region = region or os.environ.get(
            "GATEWAY_REGION",
            os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
        self._mcp_client = None
        self._tools: List = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> List:
        """Connect to the Gateway and discover MCP tools.

        Returns:
            List of MCP tool callables that can be passed to a Strands Agent.
            Returns an empty list if no Gateway endpoint is configured
            (graceful fallback for local/mock testing).
        """
        if not self.endpoint_url:
            logger.warning(
                "GATEWAY_ENDPOINT not set — running in mock mode (no MCP tools). "
                "Set GATEWAY_ENDPOINT to connect to AgentCore Gateway."
            )
            return []

        try:
            from strands.tools.mcp import MCPClient
            from mcp.client.streamable_http import streamablehttp_client

            logger.info(f"Connecting to AgentCore Gateway: {self.endpoint_url}")

            # Build SigV4-signed HTTP headers for IAM auth
            headers = self._build_auth_headers()

            self._mcp_client = MCPClient(
                lambda: streamablehttp_client(
                    url=f"{self.endpoint_url.rstrip('/')}/mcp",
                    headers=headers,
                )
            )

            # Start session and discover tools
            self._tools = self._mcp_client.start()
            tool_names = [
                getattr(t, "name", getattr(t, "__name__", str(t)))
                for t in self._tools
            ]
            logger.info(
                f"Gateway connected — discovered {len(self._tools)} tools: "
                f"{tool_names}"
            )
            return self._tools

        except ImportError as e:
            logger.warning(
                f"MCP client libraries not installed ({e}). "
                "Install 'mcp' package: pip install mcp"
            )
            return []
        except Exception as e:
            logger.error(f"Failed to connect to AgentCore Gateway: {e}")
            return []

    def stop(self):
        """Disconnect from the Gateway and clean up resources."""
        if self._mcp_client:
            try:
                self._mcp_client.stop()
                logger.info("Gateway MCP session closed.")
            except Exception as e:
                logger.warning(f"Error closing MCP session: {e}")
            finally:
                self._mcp_client = None
                self._tools = []

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> List:
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _build_auth_headers(self) -> dict:
        """Build IAM SigV4 auth headers for Gateway connection.

        Uses the Lambda execution role or local AWS credentials to sign
        the request to the Gateway endpoint.
        """
        try:
            import botocore.session
            from botocore.auth import SigV4Auth
            from botocore.awsrequest import AWSRequest

            if not self.endpoint_url:
                return {}

            session = botocore.session.get_session()
            credentials = session.get_credentials()
            
            if not credentials:
                logger.warning("No AWS credentials found for SigV4 signing")
                return {}

            # Freeze credentials to ensure we have a consistent set (including token)
            frozen = credentials.get_frozen_credentials()

            # Create a request object to sign
            # We sign the connection endpoint (usually /mcp)
            service_name = "execute-api"  # API Gateway execution service
            url = f"{self.endpoint_url.rstrip('/')}/mcp"
            
            request = AWSRequest(
                method="POST",
                url=url,
                data=b"", # SSE connection often has empty body or specific handshake
            )
            
            # Apply SigV4 signature
            SigV4Auth(frozen, service_name, self.region).add_auth(request)
            
            # Extract headers (Authorization, X-Amz-Date, X-Amz-Security-Token)
            headers = dict(request.headers)
            headers["Content-Type"] = "application/json"
            
            logger.debug(f"SigV4 headers generated for {url}")
            return headers

        except Exception as e:
            logger.warning(f"Could not build SigV4 headers: {e}")
            return {"Content-Type": "application/json"}

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def tools(self) -> List:
        """Currently discovered MCP tools (empty until start() is called)."""
        return self._tools

    @property
    def is_connected(self) -> bool:
        """Whether we have an active MCP session."""
        return self._mcp_client is not None and len(self._tools) > 0

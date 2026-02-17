"""Test script to verify IAM Auth connection to AgentCore Gateway.

Usage:
    export GATEWAY_ENDPOINT="https://your-gateway-id.execute-api.us-east-1.amazonaws.com/prod"
    export AWS_PROFILE=my-profile  # Optional
    python test_gateway_connection.py
"""
import os
import sys
import logging
import requests
import botocore.session
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("GatewayTest")

def test_connection():
    endpoint = os.environ.get("GATEWAY_ENDPOINT")
    if not endpoint:
        logger.error("GATEWAY_ENDPOINT env var is not set.")
        logger.info("Usage: set GATEWAY_ENDPOINT=https://... && python test_gateway_connection.py")
        sys.exit(1)

    region = os.environ.get("GATEWAY_REGION", os.environ.get("AWS_REGION", "us-east-1"))
    url = f"{endpoint.rstrip('/')}/mcp"
    
    logger.info(f"Target URL: {url}")
    logger.info(f"Region: {region}")

    # 1. Get Credentials via Boto3 Session
    session = botocore.session.get_session()
    credentials = session.get_credentials()
    
    if not credentials:
        logger.error("No AWS credentials found. Run 'aws configure' or set env vars.")
        sys.exit(1)

    frozen_creds = credentials.get_frozen_credentials()
    logger.info(f"Using credentials for AccessKey: {frozen_creds.access_key}")

    # 2. Prepare Request
    # Note: AgentCore Gateway MCP endpoint typically accepts POST for SSE initiation
    request = AWSRequest(
        method="POST",
        url=url,
        data=b"",
    )

    # 3. Sign Request (SigV4)
    logger.info("Signing request with SigV4...")
    SigV4Auth(frozen_creds, "execute-api", region).add_auth(request)
    
    # 4. Convert headers for requests library
    headers = dict(request.headers)
    headers["Content-Type"] = "application/json"
    
    # 5. Send Request
    logger.info("Sending request...")
    try:
        response = requests.post(
            url, 
            headers=headers,
            stream=True,  # Important for SSE endpoints
            timeout=10
        )
        
        logger.info(f"Response Status: {response.status_code}")
        
        if response.status_code == 403:
            logger.error("❌ Authentication Failed (403 Forbidden). Check IAM policies.")
            logger.error(f"Response: {response.text}")
        elif response.status_code == 404:
            logger.error("❌ Endpoint Not Found (404). Check URL path.")
        elif response.status_code >= 500:
            logger.error(f"❌ Server Error ({response.status_code}).")
        elif response.status_code == 200:
            logger.info("✅ SUCCESS! Connected to Gateway.")
            logger.info("Reading first few bytes of stream...")
            # Read a bit of the stream to confirm it's working
            for chunk in response.iter_content(chunk_size=128):
                if chunk:
                    logger.info(f"Received data: {chunk[:50]}...")
                    break
        else:
            logger.warning(f"Unexpected status: {response.status_code}")
            logger.info(f"Response: {response.text}")

    except Exception as e:
        logger.error(f"Connection failed: {e}")

if __name__ == "__main__":
    test_connection()

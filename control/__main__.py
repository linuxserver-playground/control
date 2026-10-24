"""
Serve on 0.0.0.0:8080 behind SWAG, which terminates TLS and sets X-Forwarded-*; trusting those from any
proxy is fine because the port is never published outside the compose network.

Usage: python -m control
"""

import logging

import uvicorn

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        "control.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=8080,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_config=None,
    )

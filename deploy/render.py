#!/usr/bin/env python3
"""Render the custom Compose application to submit through the TrueNAS Apps API."""

import argparse
import json
import re

ENVIRONMENTS = {
    "sandbox": ("product-finder-sandbox", "products-sandbox.stoked.tech", 8462),
    "production": ("product-finder", "products.stoked.tech", 8463),
}
PROXY_IMAGE = "quay.io/oauth2-proxy/oauth2-proxy:v7.15.6"
RELEASE = r"ghcr\.io/markstokes-outlook/product-finder/truenas(?::[0-9a-f]{40}|@sha256:[0-9a-f]{64})"


def render(environment, image):
    if not re.fullmatch(RELEASE, image):
        raise ValueError("Use a full Product Finder GHCR commit tag or a verified digest")
    app, host, port = ENVIRONMENTS[environment]
    root = f"/mnt/Applications/{app}"
    config = f"{root}/private/config.yaml:/config/config.yaml:ro"
    database = f"{root}/data:/data"
    restart = {
        "restart": "unless-stopped",
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
    }
    services = {
        "web": {
            **restart,
            "image": image,
            "environment": {"PRODUCT_FINDER_HOST": host},
            "env_file": [f"{root}/private/web.env"],
            "volumes": [database, config],
            "healthcheck": {
                "test": [
                    "CMD", "python", "-c",
                    "import os, urllib.request as u; "
                    "request = u.Request('http://127.0.0.1:8080/', "
                    "headers={'Host': os.environ['PRODUCT_FINDER_HOST']}); "
                    "u.urlopen(request, timeout=5).close()",
                ],
                "interval": "15s", "timeout": "8s", "retries": 6, "start_period": "30s",
            },
        },
        "watch": {
            **restart,
            "image": image,
            "command": ["product-finder", "-c", "/config/config.yaml", "watch"],
            "volumes": [database, config],
            "depends_on": {"web": {"condition": "service_healthy"}},
        },
        "login": {
            **restart,
            "image": PROXY_IMAGE,
            "env_file": [f"{root}/private/login.env"],
            "command": [
                "--http-address=0.0.0.0:4180",
                "--provider=oidc",
                f"--oidc-issuer-url=https://identity.stoked.tech/application/o/{app}/",
                f"--client-id={app}",
                f"--redirect-url=https://{host}/oauth2/callback",
                "--upstream=http://web:8080/",
                "--scope=openid profile email product_finder",
                "--email-domain=*",
                "--allowed-group=Product Finder Users",
                # Access is bound to Authentik group membership, independent of email.
                "--insecure-oidc-allow-unverified-email=true",
                "--insecure-oidc-skip-nonce=false",
                "--code-challenge-method=S256",
                "--cookie-name=__Host-product-finder-login",
                "--cookie-secure=true",
                "--cookie-samesite=lax",
                "--cookie-expire=8h",
                "--session-cookie-minimal=true",
                "--pass-basic-auth=false",
                "--pass-user-headers=false",
                "--skip-provider-button=true",
                "--reverse-proxy=true",
            ],
            "ports": [f"127.0.0.1:{port}:4180"],
            "depends_on": {"web": {"condition": "service_healthy"}},
        },
    }
    return {
        "services": services,
        "x-portals": [{
            "name": "Product Finder Sandbox" if environment == "sandbox" else "Product Finder",
            "scheme": "https", "host": host, "port": 443, "path": "/",
        }],
        "x-notes": f"Product Finder {environment}. Authentik sign-in through the NAS Home tunnel.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environment", choices=ENVIRONMENTS)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    print(json.dumps(render(args.environment, args.image), indent=2))

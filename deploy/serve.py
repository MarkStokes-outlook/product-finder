"""WSGI entry point behind the authenticated TrueNAS proxy."""

import os
from urllib.parse import urlsplit

from flask import abort, request

from product_finder.config import load_config
from product_finder.web.app import create_app


def hosted_app():
    host = os.environ["PRODUCT_FINDER_HOST"]
    app = create_app(load_config("/config/config.yaml"))
    app.config.update(
        SECRET_KEY=os.environ["PRODUCT_FINDER_SECRET_KEY"],
        SESSION_COOKIE_NAME="__Host-product-finder",
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="Lax",
        TRUSTED_HOSTS=[host],
    )

    @app.before_request
    def check_browser_origin():
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return
        source = request.headers.get("Origin", request.referrer or "")
        try:
            origin = urlsplit(source)
        except ValueError:
            abort(403)
        if origin.scheme != "https" or origin.netloc.lower() != host:
            abort(403)

    @app.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    return app


if __name__ == "__main__":
    from waitress import serve

    serve(hosted_app(), listen="0.0.0.0:8080", url_scheme="https")

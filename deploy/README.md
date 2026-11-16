# TrueNAS deployment

| Environment | TrueNAS app | Portal | Git branch | NAS Home origin |
| --- | --- | --- | --- | --- |
| Sandbox | `product-finder-sandbox` | [Sandbox](https://products-sandbox.stoked.tech/) | `develop` | `http://127.0.0.1:8462` |
| Production | `product-finder` | [Production](https://products.stoked.tech/) | `main` | `http://127.0.0.1:8463` |

CI runs the application tests and validates both custom app definitions. Passing builds on `develop` and `main` publish linux/amd64 images to `ghcr.io/markstokes-outlook/product-finder/truenas` with both branch and full commit tags. Deploy to sandbox first. Pull the full commit tag on NAS Home, verify the image's OCI revision label, and keep its digest and the prior app definition. Promote the tested change to `main` through a pull request, then repeat with the production build. GitHub publishes images and TrueNAS controls rollout.

## App installation

1. Create `/mnt/Applications/<app>/data` for SQLite, owned by UID/GID 10001, and a root-private `/mnt/Applications/<app>/private`. Mount a readable private `config.yaml` with `db_path: /data/product_finder.db`. Prepare any SQLite import before starting either service. Keep a consistent backup and never share a live SQLite file across hosts.
2. Provision separate confidential Authentik OIDC clients named after each app with the exact `https://<host>/oauth2/callback` redirect, authorization code with PKCE, and a signing key. Bind access to **Product Finder Users** and return the user's group in the `groups` claim from the `product_finder` scope. Put `OAUTH2_PROXY_CLIENT_SECRET` and a random 32-byte URL-safe base64 `OAUTH2_PROXY_COOKIE_SECRET` in `private/login.env`. Put a separate random `PRODUCT_FINDER_SECRET_KEY` in `private/web.env`. Keep both env files root-only.
3. Run `python deploy/render.py sandbox --image <verified-ghcr-image>` (or `production`) to obtain the Compose object. Submit it as `custom_compose_config` with `custom_app: true` and the app name through TrueNAS `app.create`. Verify the app and its portal in Apps.
4. Confirm the local proxy redirects unauthenticated requests to Authentik before adding the matching HTTPS hostname on the Home Cloudflare tunnel. Only the login proxy is published on the NAS loopback. Keep direct web and watcher ports private.
5. Verify authenticated dashboard access, anonymous denial of app data, and a successful production watcher cycle. Sandbox uses separate data and source settings. Keep production marketplace and alert credentials out of sandbox.

Before a rollout, retain the applied Compose and a consistent SQLite backup. Update the existing app in place through `app.update`. Verify its portal and environment data after the update. Roll back to the prior pinned image and Compose if the data schema remains compatible, or restore the matching protected backup if it does not.

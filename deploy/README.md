# Deployment

The screener runs as a second, isolated compose stack on the VPS that already
hosts Job Terminal. Two containers: the application, and a `cloudflared` tunnel
that reaches it over the compose network. Neither publishes a host port.

Postgres runs in the stack with a named volume. The `compose.yaml` at the
repository root is a different thing — the throwaway database the test suite
drops and recreates — and has nothing to do with this.

## What runs where

| Piece | Where it lives |
|---|---|
| Application image | `ghcr.io/d1k03/stock-aggregator`, tagged with the commit SHA and `latest` |
| Database | `postgres:16` in the stack, on the `pg_data` named volume |
| Secrets | Infisical, fetched at startup into the process environment |
| Bootstrap credentials | `${VPS_APP_DIR}/.env` on the box, root-owned `0600` |
| Ingress | Cloudflare Tunnel, hostname mapping in the Zero Trust dashboard |
| SSH | Deploys go over the tailnet; port 22 is still open publicly (see Hardening) |

## First-time setup

Each of these fails at an inconvenient hour if skipped, and none of them can be
done from CI.

1. **Tailscale.** The VPS must be on the tailnet; its tailnet IP is
   `VPS_HOST`. The ACL needs `tag:ci` in `tagOwners` and a grant letting
   `tag:ci` reach the VPS on port 22.

   For the CI credential, prefer an **OAuth client** (admin console → Settings
   → OAuth clients, scope `auth_keys` write, tag `tag:ci`) because it does not
   expire. Tailscale has no API for creating one, so an auth key is the
   fallback: reusable, ephemeral, tagged `tag:ci`, set as `TS_AUTHKEY`. If you
   use an auth key, update the expiry date in the `Check the Tailscale key has
   not expired` step of `deploy.yml` — that step is what stops an expired key
   from presenting as a mysterious network timeout.
2. **Infisical.** Create a project for the screener — its own, not a shared one
   with Job Terminal, so a leaked identity on one stack cannot read the other's
   secrets. Add a machine identity with Universal Auth, and populate every key
   in `.env.example` before the first boot. A failed fetch is fatal by design,
   so a half-populated project means a container that will not start.
3. **The env file.** On the box, `${VPS_APP_DIR}/.env` holding only
   `INFISICAL_CLIENT_ID`, `INFISICAL_CLIENT_SECRET`, `INFISICAL_PROJECT_ID` and
   optionally `INFISICAL_ENV`. These are the only credentials stored on the
   server; everything else is fetched with them and never touches disk.
4. **Database credentials.** Put `POSTGRES_USER`, `POSTGRES_PASSWORD` and
   `POSTGRES_DB` in the same `.env`. The compose file builds `DATABASE_URL`
   from them, so there is one copy of the password rather than two that can
   disagree. Neither Postgres nor the app publishes a port, so the database is
   reachable only from inside the compose network.

   `btree_gist` needs no setup — the official `postgres:16` image ships the
   contrib modules, so migration 001 enables it on first boot.
5. **Cloudflare.** Create a tunnel in Zero Trust, take its token into Infisical
   as `CLOUDFLARE_TUNNEL_TOKEN`, and add a public hostname on the
   `edenmatrix.xyz` zone routing to `http://app:8080`. Put an Access policy in
   front of it — `/status` reports which build is running.
6. **GHCR pull access.** The VPS needs to pull the image. Either make the
   package public or put a `read:packages` token on the box.
7. **GitHub secrets.** `TS_OAUTH_CLIENT_ID`, `TS_OAUTH_SECRET`, `VPS_HOST`,
   `VPS_USER`, `VPS_SSH_KEY`, `VPS_APP_DIR`.

## Deploying

Automatic. `ci.yml` calls `deploy.yml` once `ci-ok` passes on `main`, which
builds the image, publishes it, joins the tailnet, copies the compose files,
restarts the stack and waits for `/ready`.

The smoke test runs *inside* the container, not against the public hostname.
Cloudflare Access answers an unauthenticated request with a 302 to its login
page, and `curl -f` does not treat a redirect as a failure — so a public probe
would report success against a completely dead application.

## Rolling back

Run the **Deploy** workflow manually with `image_tag` set to an earlier commit
SHA. The build is skipped and the box is pointed at an image that already
exists, so a rollback takes about as long as a `docker compose pull`.

**Rollback across a migration boundary is not supported.** There are no down
migrations, and an older image will start happily against a newer schema and
behave in ways nobody has thought about. If a release added a migration, roll
forward with a fix instead.

## Checking it works

```bash
docker compose --env-file .env \
  -f deploy/compose.prod.yaml -f deploy/compose.tunnel.yaml \
  exec -T app python -m screener.boot selftest
```

This exercises each integration against the real world and reports one line
each: the database and its migration count, the build SHA, a direct fetch, a
proxied fetch and whether its exit IP actually differs from the direct one,
and an OpenRouter round trip with its cost. Anything unconfigured reports
`SKIP` rather than failing, because switched-off is the expected state for
most of it.

It does not post to Discord. Sending a message into a real channel is an
outward-facing action, and a self-test should not make one.

Nothing in the infrastructure layer has a consumer yet — ingest, scoring and
alerting are all unwritten — so this command is the only thing that would
notice a root going quietly broken.

## Running it locally

```bash
cp deploy/local.env.example deploy/local.env
docker compose --env-file deploy/local.env \
  -f deploy/compose.prod.yaml -f deploy/compose.local.yaml up -d --build
```

Then <http://localhost:8080>. Same Caddy routes and service names as the VPS,
because it is the production compose file with an override rather than a
separate one that could drift.

With the Infisical trio unset the app reads `local.env` as-is, which also
switches GitHub sign-in off, so `/status` and `/api/audit` are open. The
dashboard's own middleware still wants a session cookie though, and it is
presence-only by design, so give it one from the browser console:

```js
document.cookie = "screener_session=local; path=/"
```

`docker compose ... down -v` removes the containers and the local volume.

## Hardening still to do

`sshd` on the VPS listens on `0.0.0.0:22`, so the box accepts SSH from the
public internet as well as over the tailnet. Deploys do not need that — they
come in over Tailscale — so restricting port 22 to the tailnet interface is a
free improvement.

It is deliberately not done here, because the box hosts other services and
whoever does it should be certain they have a second way in first.

## Backups

There are none yet, and this is the gap worth closing first. The database is a
volume on a VPS, so no managed service is taking a snapshot on anyone's behalf,
and `docker compose down -v` would take the lot.

Until a schedule exists, a manual dump before anything risky:

```bash
docker compose --env-file .env -f deploy/compose.prod.yaml \
  exec -T postgres pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > screener-$(date +%F).dump
```

## Spend limits

Set them at the provider, not in code: a credit cap on the OpenRouter key and a
spend cap on the Bright Data zone. Both are enforced by someone other than this
codebase and cannot be defeated by a bug in it.

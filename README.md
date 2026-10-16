# Xeonr Home Assistant Cloud

A Home Assistant custom integration that links an instance to Xeonr's Home
Assistant cloud. It gives you:

- **Remote access.** A public HTTPS URL for this instance through a Xeonr
  Burrow tunnel. Webhooks work from the internet on that URL too.
- **Camera relays.** Short-lived TURN credentials from Xeonr's coturn,
  registered as WebRTC ICE servers.
- **Off-site backups.** A backup location for Home Assistant's own backups,
  stored in your team's storage. Only encrypted backups are accepted.

> **Status:** pre-release (0.x).

## Requirements

- Home Assistant **2026.10 or newer**, on any install type (OS, Container,
  Core, Supervised).
- [HACS](https://hacs.xyz) for the recommended install. Without it, see the
  manual install below.
- A Xeonr account in a team that has Home Assistant Cloud enabled.
- The Home Assistant Cloud **editor** or **admin** role in that team. Set it
  in the Xeonr console under your team's **Members & roles**. Being the
  team's owner isn't enough on its own.

## Install with HACS (recommended)

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=xeonr&repository=hacloud-integration&category=integration)

Click the button above and confirm, or add it by hand:

1. In Home Assistant, open **HACS**, then **⋮ → Custom repositories**.
2. Add `https://github.com/xeonr/hacloud-integration` with type
   **Integration**.
3. Open **Xeonr Home Assistant Cloud** in HACS and select **Download**.
4. Restart Home Assistant.
5. Go to **Settings → Devices & services → Add integration**, and pick
   **Xeonr Home Assistant Cloud**.

HACS offers updates as new releases come out.

## Install manually

Use this without HACS. Each release has a `xeonr_cloud.zip`:
https://github.com/xeonr/hacloud-integration/releases/latest/download/xeonr_cloud.zip

1. Download and unzip it. You get a folder called `xeonr_cloud`.
2. Copy that folder into the `custom_components` folder inside your Home
   Assistant configuration folder, creating `custom_components` if it doesn't
   exist. The result is `config/custom_components/xeonr_cloud/manifest.json`.
   On Home Assistant OS, use the Samba share, the File editor or the
   Terminal & SSH add-on to reach `/config`.
3. Restart Home Assistant and add the integration as in step 5 above.

From a terminal on the machine running Home Assistant, inside the
configuration folder (`/config` on Home Assistant OS and in the container
image):

```bash
mkdir -p custom_components && curl -fsSL https://github.com/xeonr/hacloud-integration/releases/latest/download/xeonr_cloud.zip -o /tmp/xeonr_cloud.zip && rm -rf custom_components/xeonr_cloud && unzip -q /tmp/xeonr_cloud.zip -d custom_components
```

To update a manual install, run the same command again and restart.

The source of truth is https://git.prod.wtf/xeonr/hacloud/integration.
GitHub is its mirror, and both carry the same releases.

## Setting up

The config flow does the following:

1. **Sign in.** It shows a link (`https://xnr.app/tv?code=…`) and a code.
   Approve on any device; the flow continues by itself.
2. **Pick a team** if you belong to more than one.
3. **Link.** The instance is created in that team, with its own hostname. Home
   Assistant keeps only the instance's own credential. Your Xeonr sign-in is
   used once and is not stored.
4. **External URL.** If Home Assistant has no external URL, the flow offers to
   set it to the tunnel URL, and this is on by default. That makes the
   companion app, notifications and `get_url` use it. An external URL you set
   yourself is never changed. You can change this later under the
   integration's **Configure**.

## What you get

| Entity / feature | |
| --- | --- |
| `binary_sensor.<name>_remote_access` | On while the tunnel is connected. Attributes: `url` (the public URL) and `state` (`connected`, `disconnected`, `remote_access_disabled`, `reservation_removed`, `unlinked`, `agent_unavailable`). |
| Backup location | It appears as the instance's name under **Settings → System → Backups**. Turn on encryption for it, because unencrypted backups are refused. |
| ICE servers | Used automatically by WebRTC camera streams. |

## Repairs

Problems show up under **Settings → Repairs**, not just in the log:

- **Remote access is switched off.** Someone turned it off in the Xeonr
  console. Backups and camera relays keep working. The issue clears by itself
  within a few minutes of it being turned back on.
- **Public hostname was removed.** A team admin deleted the reservation in
  Burrow. **Fix** signs you in again and relinks the instance.
- **No longer linked.** The instance was removed in the console, or its
  credential was revoked. **Fix** signs you in again and relinks. Home
  Assistant also offers a re-authentication.

## How remote access works

Requests that come through the tunnel go straight into Home Assistant's own
web server, in-process, with the real client address as the peer. They are
never proxied to `127.0.0.1:8123`. This matters for three things: the
`trusted_networks` auth provider, local-only users, and IP bans all see the
real address. `X-Forwarded-*` headers added by the gateway are stripped, so
you do not need `use_x_forwarded_for`.

TLS is terminated at Xeonr's edge, not on your instance. These tunnels are set
not to record request contents, but unlike Nabu Casa, Xeonr *could* see the
traffic.

## Configuration (optional)

Everything has production defaults. To point at staging, or to try a
different sign-in client:

```yaml
xeonr_cloud:
  auth_issuer: https://auth.xeonr.dev
  api_base_url: https://hacloud.xeonr.dev
  client_id: <device-flow client id>
```

## Development

Licensed under the Apache License 2.0 (`LICENSE`).


```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt ruff
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
python3 scripts/check_hacs.py
docker run --rm -v "$PWD":/github/workspace ghcr.io/home-assistant/hassfest
```

The tests run against `tests/fake_cloud.py`, a localhost fake of Xeonr Auth,
the instance API and presigned storage, over real HTTP. The tunnel agent is
faked at the `tunnel.TunnelAgent` seam.

**Releases.** Tag `vX.Y.Z` on GitLab, matching `manifest.json`'s version.
GitLab CI then:

- runs the checks;
- publishes the GitLab release with `xeonr_cloud.zip`;
- pushes the tag to GitHub, where `.github/workflows/release.yml` publishes
  the GitHub release that HACS installs.

`main` is mirrored the same way. Don't push to GitHub directly.

**Tunnel agent library.** [`burrow-agent-py`](https://git.prod.wtf/xeonr/burrow-agent-py)
is vendored into `custom_components/xeonr_cloud/burrow_agent/`, because HACS
cannot install a private package:

```bash
scripts/sync_burrow_agent.sh            # main, or pass a tag/commit
```

The script records the source commit in `burrow_agent/VENDORED` and lists the
library's dependencies. Any of those that Home Assistant does not already ship
must go into `manifest.json` `requirements`. `tunnel.py` is the only module
that imports the library.

The API contract is the `xeonr.hacloud.v1` and `xeonr.hacloud.private.v1`
modules in [xeonr/protocol](https://git.prod.wtf/xeonr/protocol). It is spoken
as Connect JSON with plain aiohttp; there is no protobuf runtime.

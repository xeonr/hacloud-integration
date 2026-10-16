# xeonr_cloud — the Home Assistant integration for Home Assistant cloud

Memory, plans and design for this product live in
[git.prod.wtf/xeonr/platform-docs](https://git.prod.wtf/xeonr/platform-docs)
under `homeassistant-cloud/`, not here:

- design: `homeassistant-cloud/docs/DESIGN.md` (§3 is this repo);
- deploy runbook and placeholders: `homeassistant-cloud/docs/RUNBOOK.md`;
- memory: `homeassistant-cloud/integration/memory/MEMORY.md`.

Deferred work, bugs found in passing and missing implementation go to Trellis
project **HAC** (https://trellis.prod.wtf/p/hac), not TODO comments.

## Rules that are load-bearing

- Tunnel streams go into `hass.http.runner` in-process with the real client IP
  as peer (`burrow_agent.aiohttp.AiohttpStreamHandler`). Never proxy to
  localhost (DESIGN §3.2: trusted_networks, local-only users, IP bans).
- Never store the person's Xeonr tokens. Only `instance_secret` (an `xk_` API
  key bound to the instance) is kept in the config entry.
- Never overwrite an `external_url` the user set; only ever clear one recorded
  in `entry.data["external_url_set"]`.
- Refuse unencrypted backups (`AgentBackup.protected`).
- Connect JSON over plain aiohttp; do not add a protobuf runtime.
- `tunnel.py` is the only importer of the vendored `burrow_agent`; refresh it
  with `scripts/sync_burrow_agent.sh`, never by hand edits.

## Checks

`pytest`, `ruff check .`, `ruff format --check .`, `python3 scripts/check_hacs.py`,
and hassfest (`docker run --rm -v "$PWD":/github/workspace ghcr.io/home-assistant/hassfest`).

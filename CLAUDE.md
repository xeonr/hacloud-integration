# hacloud/integration

Built to the rules in Plumb (below). Memory and design docs:
platform-documentation/homeassistant-cloud/ (this repo's notes in
`integration/`; design in `docs/DESIGN.md` section 3). Tickets: Trellis
project **HAC**.

## Shape
- A HACS-installable Home Assistant integration (`custom_components/xeonr_cloud`): Burrow remote access, TURN/ICE and encrypted off-site backups, over Connect JSON on plain aiohttp to `xeonr.hacloud.v1` / `.private.v1` (no protobuf runtime).
- Tunnel streams go into `hass.http.runner` in-process with the real client IP as peer, never proxied to localhost; `tunnel.py` is the only importer of the vendored `burrow_agent` (`scripts/sync_burrow_agent.sh`).
- Only `instance_secret` (an `xk_` API key) is stored, never the person's tokens; a user-set `external_url` is never overwritten; unencrypted backups are refused.
- GitLab is the source of truth: a `vX.Y.Z` tag matching `manifest.json` releases and mirrors to GitHub for HACS; never push to GitHub.

## Commands
`pytest`, `ruff check .`, `ruff format --check .`,
`python3 scripts/check_hacs.py`, and hassfest (see README).

## Plumb
The rules this repo is built to live in [Plumb](https://plumb.xeonr.io),
not in markdown. Read them through the Xeonr MCP gateway,
`https://mcp.xeonr.io/mcp` ([setup](https://plumb.xeonr.io/docs/code/mcp)):
`plumb_search_docs`, `plumb_list_requirements`, `plumb_get_requirement`, `plumb_get_page`. Read
the ones for this repo's application type(s) before changing it, and cite
requirement ids (`serve.healthz`), not `§` numbers.

This repo is application `integration` (device-app) of service **hacloud**
in plumb
([plumb.xeonr.io/services/hacloud](https://plumb.xeonr.io/services/hacloud)).
Where it stands: `plumb_get_matrix` filtered to `hacloud`, or `plumb_get_cell` for one
rule.

To change a rule, edit its page in Plumb. Grandfathering and exempting are
a person's call; agents propose with `plumb_propose_grandfather`.

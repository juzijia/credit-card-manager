---
name: ccm
description: Query and manage the user's Credit Card Manager (CCM) instance through its verified Agent API tool only when the user explicitly requests credit card, statement, bill, or payment operations.
---

# Credit Card Manager (CCM)

Use this skill to inspect cards, query upcoming bills, view parsed transactions, trigger email sync, and manage manual bills on the user's personal Credit Card Manager instance.

## Fixed LAN configuration

`scripts/ccm_api.py` reads `~/.agents/skills/secrets/ccm.env`. The fixed LAN base URL, Host header, and Agent Token are kept there.

The tool enforces requests against private LAN addresses and passes authentication via `Authorization: Bearer <AGENT_TOKEN>`. Never print, expose, or log the configured Agent Token in commands, prompts, or reports. Use [references/api.md](references/api.md) as the route contract.

## Workflow

1. Run `python3 scripts/ccm_api.py doctor` before the first CCM operation in a turn.
2. For routine queries, use `get`. For JSON state changes, use the HTTP verb that matches the allowlist: `post`, `patch`, `put`, or `delete`.
3. For statement file uploads, use `upload --path <known-path> --file <path>` and add repeated `--query KEY=VALUE` options only when the API route documents query parameters.
4. Check [references/api.md](references/api.md) for the exact routes permitted under the server-side Agent allowlist; do not call routes outside it.

## Operational Boundaries

- **Money representation**: CCM handles money in **integer minor units** (e.g. `10000` = 100.00 CNY). Never send floating-point values for amounts.
- **Agent authorization scope**: Agents have access to query and routine action endpoints. Owner-only operations (login sessions, Agent Token management, WebDAV backups, card deletion) are strictly blocked by the server.
- **Verification**: After triggering sync or marking a bill as paid, always re-query the target endpoint to confirm state before reporting success to the user.

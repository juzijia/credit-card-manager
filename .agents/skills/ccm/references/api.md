# Credit Card Manager (CCM) Agent API Reference

All requests must be made via `scripts/ccm_api.py` using the configured Agent Token.
Agents have a fixed single permission profile defined by the server-side route allowlist (`AGENT_ROUTE_ALLOWLIST`).

---

## 1. Allowed Route Endpoints

### Cards
- `GET /api/v1/cards` - List all tracked credit cards.
- `GET /api/v1/cards/{card_id}` - Get details of a specific card.
- `GET /api/v1/cards/{card_id}/interest-free-estimate` - Estimate interest-free days and repayment periods.
- `GET /api/v1/cards/calendar-estimates` - View estimated calendar timeline for cards.
- `PATCH /api/v1/cards/{card_id}` - Update card properties (e.g. `statement_day`, `due_day`, `grace_period_days`, `annual_fee_minor`, `annual_fee_policy`, `reminder_enabled`).
- `GET /api/v1/cards/rules` - View bank statement matching rules.

### Catalog (Card Dictionary & Assets)
- `GET /api/v1/catalog/stats` - Overview statistics of the bank & card dictionary.
- `GET /api/v1/catalog/banks` - List supported banks in the official dictionary.
- `GET /api/v1/catalog/products` - List catalog card products and tiers.
- `GET /api/v1/catalog/change-requests` - View dictionary change requests.
- `POST /api/v1/catalog/scan-runs` - Trigger a card catalog scan run.
- `GET /api/v1/catalog/scan-runs` - List catalog scan history.
- `POST /api/v1/catalog/ingest` - Ingest updated dictionary metadata.

### Statements & Repayments
- `GET /api/v1/statements` - List credit card statements.
- `GET /api/v1/statements/{statement_id}` - Get a statement and its line items.
- `GET /api/v1/statements/{statement_id}/analysis` - Detailed financial analysis of a specific statement.
- `GET /api/v1/statements/marked-paid` - List statements marked as paid.
- `POST /api/v1/statements/{statement_id}/mark-paid` - Mark statement as fully paid.
- `DELETE /api/v1/statements/{statement_id}/repayments/{repayment_id}` - Delete a recorded repayment.

### Manual Bills & Mortgage
- `GET /api/v1/manual-bills` - List recurring manual bills.
- `GET /api/v1/manual-bills/current` - Current cycle manual bills.
- `GET /api/v1/manual-bills/marked-paid` - List manual bills marked paid.
- `POST /api/v1/manual-bills` - Create a new recurring manual bill.
- `PUT /api/v1/manual-bills/{bill_id}` - Update an existing manual bill.
- `DELETE /api/v1/manual-bills/{bill_id}` - Delete a manual bill.
- `POST /api/v1/manual-bills/{bill_id}/mark-paid` - Mark manual bill as paid for current cycle.
- `POST /api/v1/manual-bills/{bill_id}/unmark-paid` - Unmark paid status for manual bill.
- `POST /api/v1/mortgage/save-bill` - Save a mortgage bill item.
- `GET /api/v1/mortgage/plans` - Query mortgage calculation plans.

### Due & Dashboard & Analytics
- `GET /api/v1/due` - Query upcoming due dates, amounts, and payment reminders.
- `GET /api/v1/dashboard` - Query high-level dashboard metrics (cards, outstanding amounts, bills).
- `GET /api/v1/analytics` - Aggregate monthly spending and trend analytics.
- `GET /api/v1/transactions` - Query parsed transactions across statements.
- `GET /api/v1/transactions/{transaction_id}/interest-free` - Query interest-free calculation for a specific transaction.

### IMAP & Sync
- `GET /api/v1/imap/status` - Check IMAP connection status and scheduled poll settings.
- `GET /api/v1/imap/runs` - Query recent email sync execution runs.
- `POST /api/v1/imap/sync` - Trigger incremental background IMAP sync.
- `POST /api/v1/imap/config` - Update schedule parameters (`enabled`, `schedule_type`, `poll_seconds`, `daily_time`, `cron_expr`). *Credentials are read-only to Agent.*

### Imports
- `GET /api/v1/imports/providers` - List available bank statement parser providers.
- `GET /api/v1/imports/batches` - Query recent file import batches.
- `GET /api/v1/imports/batches/{batch_id}` - View detailed records of an import batch.
- `POST /api/v1/imports/inspect` - Inspect statement file structure.
- `POST /api/v1/imports/eml` - Upload and import .eml statement file.
- `POST /api/v1/imports/file` - Upload and import PDF / statement file.

### Notifications
- `GET /api/v1/notifications/settings` - Get notification channel configuration status.
- `POST /api/v1/notifications/trigger` - Trigger scheduled reminder cycle manually.
- `GET /api/v1/notifications/logs` - Query notification delivery logs.

---

## 2. Forbidden Operations for Agents
Agents are strictly denied from:
- Owner login / Session management (`/api/v1/auth/*`).
- Agent Token rotation / reveal (`/api/v1/agent-token/*`).
- WebDAV Backup & Restore (`/api/v1/backups/*`).
- Direct mailbox credential updating.
- Card deletion.
- Notification channel credential saving / testing (`POST /api/v1/notifications/settings`, `POST /api/v1/notifications/test`).


# Northflank 24/7 Deployment Plan

This document records the intended final deployment direction for the MT5 crypto bot. It should be treated as the planning handoff for Prompt 23 in `codex_mt5_crypto_implementation_playbook.md`.

## Objective

Run the bot 24/7 without relying on the user's laptop staying open, while preserving:

- `rules.md` as the highest-priority source of truth;
- the five-symbol allow-list: `BAR/USD`, `BTC/USD`, `ETH/USD`, `SOL/USD`, `XRP/USD`;
- conservative polling;
- approval-gated live execution;
- no fully autonomous live parameter changes;
- no committed secrets.

## Current Constraint

The current local live runner uses the `MetaTrader5` Python package and a Windows MetaTrader 5 terminal. A normal Northflank service or job should be assumed to run in a Linux container unless verified otherwise, so the local MT5 terminal route is not directly portable to Northflank.

## Preferred Final Architecture

Use Northflank for the always-on worker, database, logs, and dashboard, and use a verified MT5 cloud bridge for broker connectivity.

```text
Northflank worker/job
  -> strategy/risk/storage pipeline
  -> cloud MT5 bridge adapter, for example MetaApi
  -> private MT5 competition server
  -> Postgres audit store on Northflank
  -> read-only dashboard/report service on Northflank
```

MetaApi is the likely bridge because public docs describe MT5 account provisioning and custom provisioning profiles that can upload an MT5 `servers.dat` file. The competition server is private, so the `servers.dat` file must be captured from a Windows MT5 terminal that has connected to the competition server once.

## Fallback Architecture

If the MT5 cloud bridge cannot be verified in time:

```text
Windows MT5 host
  -> local scripts/run_bot_live.py guarded runner
  -> sync/audit writes to Northflank Postgres
  -> Northflank dashboard and analytics jobs
```

This fallback improves observability and demo readiness but does not remove the laptop dependency unless the Windows host is an always-on cloud machine.

## Northflank Components

| Component | Purpose | Notes |
| --- | --- | --- |
| Postgres addon | Durable audit store for signals, risk checks, orders, fills, positions, and account snapshots | Use environment variables from a Northflank secret group. |
| Worker service or scheduled job | Runs data collection, strategy, risk, and cloud bridge execution | Must be bounded or supervised and approval-gated. |
| Read-only dashboard/report service | Judge demo, analytics, current state, audit trail | Must never expose secrets. |
| Cron job | Offline analytics/report generation | Use non-overlapping concurrency policy. |
| Secret group | MT5/cloud bridge credentials, database URL, optional sponsor keys | Never store these in repo files. |

## Required User-Side Artifacts

These must be created outside the repo:

- Northflank account/project.
- Northflank Postgres addon.
- Northflank secret group.
- MT5 cloud bridge account, if using the preferred route.
- Cloud bridge API token.
- Cloud bridge account ID/region.
- MT5 `servers.dat` captured from a terminal connected to the private server, if using MetaApi/custom provisioning.
- MT5 login, password, and server details stored only as Northflank secrets.
- A live approval mechanism equivalent to `LIVE_APPROVED=true` and `config/LIVE_APPROVED.json`, implemented as secret/env + mounted/generated runtime artifact or stricter service-level gate.

## Prompt 23 Implementation Requirements

Prompt 23 should:

1. Research current official Northflank docs for services, jobs, cron jobs, secrets, Postgres addons, and persistent storage.
2. Research the selected MT5 cloud bridge docs, especially provisioning profiles and `servers.dat` if using MetaApi.
3. Add a small adapter boundary so strategy/risk/storage can use local MT5 or cloud MT5 bridge behind the same safety contract.
4. Add optional cloud bridge configuration and `.env.example` placeholders only.
5. Add mocked tests proving cloud live mode fails closed without approval and never sends after a failed preflight/order-check-equivalent.
6. Add Northflank deployment artifacts and exact setup docs.
7. Keep local `scripts/run_bot_live.py` working.
8. Do not run live orders.

## References

- Northflank Postgres addon docs: https://northflank.com/docs/v1/application/databases-and-persistence/deploy-databases-on-northflank/deploy-postgresql-on-northflank
- Northflank scheduled jobs docs: https://northflank.com/docs/v1/application/run/run-an-image-once-or-on-a-schedule
- Northflank secrets docs: https://northflank.com/docs/v1/application/secure/inject-secrets
- Northflank persistent storage docs: https://northflank.com/docs/v1/application/production-workloads/persistent-storage-in-production
- MetaApi provisioning profile file upload docs: https://metaapi.cloud/docs/provisioning/api/provisioningProfile/uploadFilesToProvisioningProfile/
- MetaApi account creation docs: https://metaapi.cloud/docs/provisioning/api/account/createAccount/

## Needs Verification

- Exact cloud bridge SDK/API calls for placing MT5 orders, retrieving ticks/bars/positions/history, and performing an order-check-equivalent preflight.
- Whether the competition private MT5 server works through MetaApi after uploading `servers.dat`.
- Whether Northflank runtime limits and credit allocation can support the desired worker cadence for the full competition window.
- Best representation of the approval artifact in Northflank without committing `config/LIVE_APPROVED.json`.

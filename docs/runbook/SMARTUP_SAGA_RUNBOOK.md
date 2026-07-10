# Smartup per-deal saga

## Safety boundary

Phase 10 is synthetic-only. `SMARTUP_AUTO_IMPORT_SAGA_MODE=shadow|enforced` requires an explicitly injected fake client (`smartup_saga_fake = True`). The default and rollback value is `disabled`. No real Smartup, SkladBot or Telegram call is part of the phase verifier.

Runtime flags remain independent:

- `SMARTUP_AUTO_IMPORT_BACKEND_IMPORT_ENABLED` controls local import;
- `SMARTUP_AUTO_IMPORT_CHANGE_STATUS_ENABLED` gates the status workflow;
- `SMARTUP_AUTO_IMPORT_SAGA_MODE=disabled|shadow|enforced` selects legacy, observation or saga ownership;
- `SMARTUP_AUTO_IMPORT_PROCESS_SKLADBOT_NOW=false` keeps durable create events queued instead of processing them immediately.

Rollback for this phase: set saga mode to `disabled`. A production rollout is not authorized here.

## Durable state machine

Each deal has one `pending_events` row with event type `smartup_deal_saga`. Its stable workflow identity is a SHA-256 digest of export date, scheduled slot, target delivery date, deal ID and target status. Import part, workbook hash and import UUID do not affect the identity, so a retried scheduled slot cannot create another workflow.

| State | Durable fact | Allowed next step |
|---|---|---|
| `intent_persisted` | Import snapshot and target status committed | Commit `remote_write_started`, then fake write |
| `remote_write_started` | A write may already have reached Smartup | Read-reconcile; never write blindly |
| `remote_failed` | Per-deal result was negative or ambiguous | Read-reconcile; retry only after a concrete non-target status |
| `remote_confirmed` | Write response or read confirms target | Queue/find the SkladBot create event |
| `skladbot_pending` | Target is confirmed but exactly one key is not yet durable | Retry local queueing |
| `skladbot_queued` | Exactly one durable SkladBot key is linked | Complete |

Shadow mode records `shadow_intent -> shadow_observed` while the legacy fake flow owns the decision. Disabled mode creates no saga rows.

## Failure boundaries

| Injected boundary | State after failure | Recovery |
|---|---|---|
| import → intent | Original Smartup import and its slot/deal metadata remain committed; saga transaction is absent | Retry discovers the orphan import before export, reuses its import UUID and creates the same stable workflow |
| intent → Smartup | `remote_write_started`, remote call count 0 | Read current status, then write only if a concrete non-target status is observed |
| Smartup → local | `remote_write_started`, remote may already be target | Read target, record `remote_confirmed`, write count remains unchanged |
| local → SkladBot | `remote_confirmed` | Create/find the existing stable SkladBot event and link its key |
| partial batch | One explicit state/result per deal | Resume only unresolved deals |

An empty or inconclusive reconciliation result stays `remote_failed`; it does not permit a duplicate status write.

## Evidence handling

Saga reports expose hashes of workflow keys, counts and states. Tests may compare the actual local event key in memory, but transcript evidence must use only its SHA-256 hash or a redacted prefix. Raw credentials, auth headers and production deal data are forbidden.

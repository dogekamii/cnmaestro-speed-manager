# Architecture

## Boundaries

`operations_toolkit.core` is vendor-neutral: module contract/context, session freeze gate, validation, secret redaction, and update integrity policy. `operations_toolkit.registry` explicitly composes first-party providers. It does not scan entry points or load runtime third-party plugins.

`operations_toolkit.modules.cnmaestro` owns cnMaestro details:

- `models.py` — frozen rates, inventory snapshots, immutable plans.
- `catalog.py` / `config/` — versioned package/template catalog and deny/batch/canary policy.
- `api.py` — structured pull-config parser, HTTPS/redirect checks, bounded safe GET retries, single-attempt PUT semantics.
- `adapters.py` — one small async contract with live and deterministic network-impossible demo parity.
- `planning.py` — exact DL+UL matching, immutable fingerprints, invalidation, rollback eligibility.
- `persistence.py` — SQLite WAL schema, versioned transactional migrations and per-device state machine.
- `publishing.py` — durable-before-PUT orchestration, exact stale check, strong job validation, delayed verification, canary/failure stopping.
- `ui.py` — first-party module views and audit/recovery export.

The Tk shell is in `operations_toolkit.ui`. `cli.py --smoke-test` intentionally avoids GUI construction and network imports/operations beyond normal package loading.

## Durable write lifecycle

A run and every `planned` device row are committed before preflight or PUT. Immediately before PUT, live exact rates must equal previewed exact rates. The row becomes `submitting` before the one allowed PUT attempt. A network loss around PUT is `unknown`, never definite failure. A returned job ID moves through `submitted` and `job_known`; only a completed job with success=1, failed=remaining=skipped=0 and matching target metadata (when available), followed by exact live rate verification, becomes `verified`.

## Schema recovery

`PRAGMA user_version` controls ordered migrations. Existing databases are backed up as `.v<old>.bak` before migration. Migrations use `BEGIN IMMEDIATE`/rollback; unsupported future schemas fail closed. WAL + `synchronous=FULL` prioritizes recoverability.


## Async and stale-state safety

All live adapter coroutines run on one dedicated event-loop worker; Tk only polls futures and never performs network operations on the UI thread. Plans bind a hashed connection identity and the full selected inventory fingerprint. Immediately before the first PUT, the publisher re-reads inventory and revalidates identity, exact rates, online state, scope, and current catalog target policy.

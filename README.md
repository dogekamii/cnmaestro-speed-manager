# Operations Toolkit

**Version 2.0.0-beta.1** — a modular, safety-first Windows desktop for deliberate infrastructure operations. The first first-party provider is **cnMaestro Bulk Speed Changes**. The established v1.1.0 Speed Manager remains the stable baseline; this beta does not replace or delete it.

> The beta EXE is unsigned and may trigger Microsoft SmartScreen. There is no silent installer or silent update. Verify `SHA256SUMS` before opening a downloaded executable.

## What is included

- Polished dark Tkinter/ttkbootstrap shell with static left navigation, connection/status header, module cards, status badges, progress, and accessible confirmations.
- Small documented first-party module contract; no runtime third-party plugin discovery.
- Deterministic `--demo` mode that uses an in-memory adapter and cannot make network requests.
- cnMaestro Bulk Speed Changes: exact DL+UL matching, immutable previews, protected scopes, maximum batch size, one-device canary, explicit remainder approval, failure threshold, and stop-on-first-issue.
- SQLite WAL write-ahead state machine: `planned → submitting → submitted → job_known → verified`, plus `failed` and `unknown`.
- Audit/recovery viewer and CSV/JSON exports with exact before/target/verified rates and scope.
- Safe GET retries with bounded exponential backoff/jitter and shared 429 handling. PUT is attempted once; timeout/connection loss is `UNKNOWN` and requires reconciliation.

## Install and run from source

Requires Python 3.11+.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m operations_toolkit --demo
```

Live mode:

```powershell
python -m operations_toolkit
```

Credentials are kept only in memory, cleared from the secret field after connection, and cleared with tokens when a session closes. Only HTTPS endpoints are accepted; localhost HTTP is available solely through explicit test-only APIs. Token redirects must match the authentication host or configured allowlist.

## Safe operating flow

1. Configure `packages.json` in the application data directory. Set `protected_scopes` (`network:…`, `tower:…`, `ap:…`), `max_batch_size`, canary size/failure policy.
2. Connect. Connection switching is frozen while scan, publish, or reconciliation is active.
3. Scan, select devices, and create an immutable preview. Any target, selection, rates, package, online state, or scope change invalidates it.
4. Review exact DL/UL before and target rates. Approximate matching is informational and needs explicit per-device acknowledgement before planning.
5. Type `APPLY SPEED CHANGES`, approve the canary flow, and confirm. There is intentionally no publish-cancel button: queued work can stop before the next device, but an in-flight PUT continues.
6. Review Audit & Recovery. Never resubmit an `UNKNOWN` write; reconcile it first.

See [Operator runbook](docs/OPERATOR_RUNBOOK.md), [Architecture](docs/ARCHITECTURE.md), and [Module contract](docs/MODULES.md).

## Rollback

Every plan captures exact previous rates and the previous known template. A rollback plan may include only a **verified** prior change whose exact previous DL+UL maps to a validated catalog template. Unmatched prior configurations are marked not automatically rollbackable. Rollback uses the same documented template operation and must pass normal preview/canary/stale-check/verification controls; no undocumented API is assumed.

## Updates

Update metadata must use HTTPS and the approved `dogekamii/cnmaestro-speed-manager` GitHub repository. An EXE is not opened until its SHA-256 matches metadata. Installation is always operator initiated. The existing root `latest.json` is intentionally preserved for the v1 stable line; beta metadata is shown under `release/`.

## Recovery and evidence limits

The WAL database is durable operational evidence and supports restart visibility for `unknown`, `submitted`, and `job_known` records. Versioned migrations are transactional and create a pre-migration backup. It is **not tamper-proof compliance evidence**; export records to an appropriately controlled evidence system when required.

## Development

```bash
python -m pip install -e '.[dev]'
xvfb-run -a pytest --cov=operations_toolkit
ruff check .
mypy src/operations_toolkit
python -m operations_toolkit --smoke-test
pyinstaller --clean --noconfirm OperationsToolkit.spec
./dist/OperationsToolkit --smoke-test  # Linux validation; CI runs OperationsToolkit.exe on Windows
```

All cnMaestro API tests use fixtures/fakes. Do not point tests at a live tenant. Build outputs, databases, credentials, the v1 ZIP/reference, and exports are gitignored.

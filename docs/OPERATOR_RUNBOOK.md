# Operator runbook

## Before a run

- Back up the app data directory and inspect package catalog schema/version.
- Verify exact templates against the tenant; configure protected network/tower/AP rules and maximum batch size.
- Start with `--demo` for training. Demo displays `DEMO · Network disabled` and cannot instantiate the live adapter.
- Treat approximate matches as informational. Explicitly acknowledge each unmatched device or exclude it.

## Publish

- Scan immediately before preview; review exact DL/UL and scope.
- Create preview after final selection/target. Any relevant change requires a new preview.
- Run the configured canary first (one device by default). The toolkit offers explicit remainder approval only after every canary device verifies. Any failed, unknown, skipped, target-mismatched, or verification-mismatched canary blocks the remainder regardless of the later-batch failure threshold.
- Do not attempt to cancel an in-flight write. Stopping means “before next device” only.

## Outcome meanings

- `verified`: strong job checks passed and exact target rates were re-read.
- `failed`: definite pre-write drift, job failure/skip/target mismatch, or verification mismatch.
- `unknown`: cnMaestro may have accepted the PUT, job remains ambiguous, or polling timed out. **Do not resubmit.**
- `planned`: not yet submitted, often because canary remainder was declined/stopped.

## Restart/reconciliation

Open Audit & Recovery and inspect `submitting`, `unknown`, `submitted`, and `job_known`. A crash-window `submitting` record is ambiguous and must never be automatically resubmitted. If a job ID exists, check that exact job and verify live DL/UL. A PUT timeout without job ID requires independent tenant inspection; never infer failure or resend automatically. Export CSV/JSON before manual intervention and record the operator decision externally.

## Rollback

Generate rollback eligibility from a verified source run. Only records whose exact previous DL+UL map to a known catalog template are eligible. Unmatched records require a manually reviewed, documented method; the toolkit does not invent template/API behavior.

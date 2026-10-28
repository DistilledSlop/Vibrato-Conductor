# Operator guide

## Starting and watching a fleet

Start `python3 -m conductor serve --config FILE`. The default binds `0.0.0.0` (all IPv4 interfaces). Open `http://HOST:8080/` using the server’s reachable address; `--host` can select a specific interface. Use the token file printed by the service to unlock the dashboard. The API supports a bearer token for command-line use; browser sessions use HttpOnly, SameSite cookies. The API rejects cross-origin mutations and serves artifacts only within their run directories. A controller restart may require signing into the browser console again; worker state and desired count persist independently.

The default Gallery view shows real client frames, task context, world, lifecycle, tick cadence, capture bytes, and memory. Visible thumbnails refresh every 15 seconds, while frames outside the viewport do not fetch until scrolled into view. The larger inspection screen refreshes at the same interval and includes client/server/proxy/display logs, actual mode and world information, and metadata/result/manifest links. Stopped runs show retained diagnostic frames. All runs and Needs attention filters keep failed workers inspectable after replacement.

The target is a desired count, bounded by the configured measured ceiling, CPU, memory, temporary storage, available disk, pressure, and artifact buffering. Starts are staggered. A worker becomes healthy only after world readiness, warmup, fresh client/server/capture signals, and sufficient measured tick samples. Cumulative tick counters and elapsed time avoid bias from sampling short instantaneous rates. Raw startup telemetry remains retained.

Stopping a worker reduces the target; replacing it keeps the target and uses a new run id. Stop and replace persist intent before the backend acts. Setting the target to zero drains workers. Stopping/restarting the controller itself preserves workers for adoption. A second controller sharing local state cannot create duplicate runtimes through the reconciler lock; use one controller replica with persistent SQLite state and compatible filesystem locking.

Failure circuits pause repeated replacements instead of burning through worlds indefinitely. After addressing the cause, use Retry after fix or authenticated `POST /api/fleet/resume`. This resets failure delays without granting more capacity, changing desired count, or bypassing storage/acceptance gates.

## Configuration

`deploy/fleet.docker.example.json` and the Kubernetes ConfigMap are reviewable starting points. Acceptance defaults false, target zero, and maximum workers one. Paths for Docker bind mounts must refer to the daemon host; mount the same absolute paths if containerizing a Docker controller. Kubernetes needs shared artifact/world storage suitable for its nodes; one-node hostPath test claims are not a multi-node storage solution.

Fleet settings include `fleet_id`, backend, persistent state/artifact/workspace paths, central delivery path, resource budgets, explicit worker limits, start pacing, health/startup timeouts, task catalog, enabled task IDs, and world bank IDs. `mode` selects diagnostic or collection; `gamemode` and operator permissions belong to each scenario assignment. `worker_env` can explicitly configure rendering threads and settle/warmup budgets; selected settings are recorded in run metadata.

Set `max_workers=0` to remove the worker-count ceiling. Set `cpus=0` and `memory_mb=0` to omit container CPU and memory quotas, corresponding Kubernetes requests/limits, and their fleet admission/host-pressure gates. CPU/memory telemetry and tick-health checks remain active. Positive values retain bounded behavior. JVM client/server heap settings are independent application settings.

With an unbounded worker count, storage still limits admission. Set `storage_budget_mb=0` to use available disk after the reserve rather than a fixed aggregate fleet budget. Per-run `storage_mb`, artifact buffering, delivery backpressure, disk reserve, and startup pacing still apply. The dashboard accepts any nonnegative integer target when the count ceiling is zero; it shows the current storage capacity separately.

For a supplied bank, set:

```json
{
  "world_bank": "/opt/world-bank",
  "world_ids": ["world1", "world2"],
  "allow_world_copy": true,
  "worker_env": {"LP_NUM_THREADS": "2", "SCREENSHOT_SECONDS": "15"}
}
```

The full bank contains 50 entries. Restrict IDs for repeatable diagnosis, then expand suitable entries for coverage. Optional `world.json` tags express surveyed prerequisites; unverified biome/structure guesses should not be asserted as facts. `gamemodes` can restrict allowed assignments and `allowOperator=false` can prohibit privileged scenarios. Default templates permit explicit isolated overrides into all four modes. The source seed remains distinct from the bot's scheduling/behavior seed.

The default catalog includes long budgets for `@nether` and `@gamer`, rather than treating them like short gathering tasks. These goals must actually complete before being labelled successful. A healthy time-bounded run alone does not establish progression success. Controlled orchestration tests can use a short, confirmed gathering task plus a sufficient post-task settle interval, rather than depending on a long autonomous adventure.

## Artifact delivery and retention

Workers stream raw captures, indexes, logs, heartbeats, resource samples, screenshots, outcomes, and metadata to the spool (`artifacts_dir`). A finalized run becomes immutable when its checksummed manifest is published last. The independent delivery queue verifies the spool, stages a full or compact central record, verifies it, and atomically publishes it before writing its admission receipt. Durable receipts and retries survive controller restarts. An interrupted or mismatched copy cannot become an admitted dataset entry.

For new fleet workers, `compact_retention` defaults to true. The controller persists an activation cutoff in `state_dir/compact-retention.json`; only known workers created at or after that cutoff are selected. Existing workers, queued runs, imports and unknown directories retain the full-run policy until separately migrated. Set `compact_retention` to false to disable selection for new runs. Active and undelivered artifacts remain intact while the worker is running or delivery is incomplete.

After independent assessment of a finalized full source, an eligible compact central record keeps raw capture `.bin` and `.jsonl` index pairs and their `.status.json` flush markers, `metadata.json`, `result.json`, a copy of the original manifest as `source-manifest.json`, `validation-evidence.json`, `coverage-projection.json`, and any present `script-result.json`, `activity-result.json`, `scenario-result.json`, `packet-coverage.json`, `source-world-metadata.json`, and `earned-context-migration.json`. Its own manifest binds these files; a separate admission receipt binds the original manifest, compact manifest, and validation evidence. Ineligible compact runs retain only a small outcome record and receipt, without capture or world payload. Once delivery and coverage ingestion are complete for a terminal worker, the temporary full spool and workspace can be removed. Cleanup uses same-filesystem tombstone renames and retries interrupted deletion.

Previously delivered runs can be reviewed with `python3 -m conductor compact-history --config CONFIG --limit 10`; add `--apply` to migrate that bounded batch. The migration selects known terminal workers with ingested coverage and verified full delivery and admission; active, unknown, undelivered, changed or stale records are blocked or skipped. Add `--include-imports` to include managed imports whose delivered manifest matches the coverage inventory, after a fresh backend check confirms no active runtime with that ID. A per-run journal and verified backup permit interrupted conversions to resume. The original manifest hash remains a valid earned-context reference only while the compact source and current admission remain valid. Run-world workspaces may be removed after verified conversion; the world bank and existing template source evidence are not removed by this operation.

Deploy a worker image with compact lineage support before converting an earned-template source. Docker workers need read-only access to the central directory. For Kubernetes, configure `central_pvc` to mount the central dataset read-only; if the mount is unavailable, earned-context validation fails closed. Existing templates continue to require their original bank world and copied provenance evidence. Compact records cannot supply the discarded raw observer evidence required to create new templates.

Compact admission trusts the controller's receipt-bound assessment of the full source. It verifies retained bytes, capture integrity, current receipt and earned-context source admission on export, but cannot independently repeat checks against discarded full logs and server heartbeats. A later quality-policy version requiring discarded evidence cannot re-audit the record and must fail closed. Compact runs do not retain worlds or original server-observer histories needed to create new earned inventory templates; prepare such templates from a separately preserved full source and world before using compact retention.

`central_dir` is independent of disposable workers and SQLite control state. A filesystem mount is the selected backend; it can be a shared storage mount. Object-store credentials and a specific external object-storage service are not assumed. The store bounds live spool and central space and inhibits new starts when buffering/delivery cannot keep up. Already-running workers continue to be supervised.

For full-run records, local spool retention, mutable-world retention, ordinary diagnostic retention, and longer failure retention are separate settings. Cleanup protects active/undelivered/unverified evidence, rejects symlink/path escapes, and verifies central identity before deleting a redundant local copy. Eligible training entries are never automatically expired by diagnostic retention. Runtime containers/Pods also have a bounded retention count and require verified local or central evidence before cleanup.

SIGTERM stops the client, drains the proxy, sends the server's console `stop`, and saves the world before finalization. Failed diagnostics can include a bounded world archive: regular files only, explicit logical/output/time limits, and a recorded reason when skipped. SIGKILL cannot run this cleanup. The controller finalizes available partial files as an invalid forensic result only after the runtime is terminated; compact retention removes that payload after terminal delivery; Kubernetes API absence is treated conservatively with reserved capacity, stale-heartbeat waits, and artifact quiescence. Missing-process inference is labelled inferred, not confirmed task failure.

## Coverage and quality

Coverage counts include both observable presence and explicitly missing interaction facets. Inventory presence is not acquisition; looking at a block is not interacting with it; a nearby mob is not a successful fight. Server-confirmed controlled actions credit only their specific targets. Requested but unsupported targets stay visible. Failures and cooldowns prevent one repeatedly failing task from consuming the fleet indefinitely.

A run can provide useful diagnostic observations while remaining excluded from training. The authoritative delivery admission is distinct from the immutable worker verdict: central `.admission/RUN_ID.json` receipts and the delivery database hold the current policy decision. Policy upgrades re-audit older delivered entries in bounded batches; never train from `result.json.datasetEligible` alone. Native-thread exhaustion in client/server logs excludes a run even if the task later completed. Final admission independently checks artifact integrity, actual task outcome, mode/permissions, client and server capacity samples, software/world identities, complete capture indexes, and watchdog/experimental flags. Long-task timeouts, cancelled tasks, inferred behavior failures, resource overload, missing server telemetry, and unknown outcomes are not successes.

## Reproducible checks

Build the current mod and images, then run the unit suite. The live scripts exercise the isolated test cluster/fleet only and require explicit exercise/acceptance flags. They create real Minecraft processes and preserve failed as well as passed evidence.

```bash
python3 scripts/test-worker-live.py --id example-unique-id --task '@get dirt 1' \
  --seconds 90 --mode collection --expect eligible --cpus 8 --render-threads 2 \
  --accept-eula --accept-offline
python3 scripts/test-docker-fleet-live.py --exercise --config .state/gallery/config.json
python3 scripts/test-kubernetes-live.py --exercise --accept-eula --accept-offline
```

Optional browser QA uses the development-only Playwright dependency:

```bash
python scripts/test-dashboard-browser.py --url http://127.0.0.1:8080 \
  --token-file PATH --with-workers
```

Use fresh single-run IDs and separate state for workload/performance experiments. Preserve failed measurements rather than presenting a smaller or stationary task as proof that every workload sustains the same concurrency.

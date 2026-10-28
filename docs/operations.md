# Operator guide

## Starting and watching a fleet

Start `python3 -m conductor serve --config FILE`. The local default binds loopback. Use the token file printed by the service to unlock the dashboard. The API supports a bearer token for command-line use; browser sessions use HttpOnly, SameSite cookies. The API rejects cross-origin mutations and serves artifacts only within their run directories. A controller restart may require signing into the browser console again; worker state and desired count persist independently.

The default Gallery view shows real client frames, task context, world, lifecycle, tick cadence, capture bytes, and memory. Visible thumbnails refresh every 15 seconds, while frames outside the viewport do not fetch until scrolled into view. The larger inspection screen refreshes at the same interval and includes client/server/proxy/display logs, actual mode and world information, and metadata/result/manifest links. Stopped runs show retained diagnostic frames. All runs and Needs attention filters keep failed workers inspectable after replacement.

The target is a desired count, bounded by the configured measured ceiling, CPU, memory, temporary storage, available disk, pressure, and artifact buffering. Starts are staggered. A worker becomes healthy only after world readiness, warmup, fresh client/server/capture signals, and sufficient measured tick samples. Cumulative tick counters and elapsed time avoid bias from sampling short instantaneous rates. Raw startup telemetry remains retained.

Stopping a worker reduces the target; replacing it keeps the target and uses a new run id. Stop and replace persist intent before the backend acts. Setting the target to zero drains workers. Stopping/restarting the controller itself preserves workers for adoption. A second controller sharing local state cannot create duplicate runtimes through the reconciler lock; use one controller replica with persistent SQLite state and compatible filesystem locking.

Failure circuits pause repeated replacements instead of burning through worlds indefinitely. After addressing the cause, use Retry after fix or authenticated `POST /api/fleet/resume`. This resets failure delays without granting more capacity, changing desired count, or bypassing storage/acceptance gates.

## Configuration

`deploy/fleet.docker.example.json` and the Kubernetes ConfigMap are reviewable starting points. Acceptance defaults false, target zero, and maximum workers one. Paths for Docker bind mounts must refer to the daemon host; mount the same absolute paths if containerizing a Docker controller. Kubernetes needs shared artifact/world storage suitable for its nodes; one-node hostPath test claims are not a multi-node storage solution.

Fleet settings include `fleet_id`, backend, persistent state/artifact/workspace paths, central delivery path, resource budgets, explicit worker limits, start pacing, health/startup timeouts, task catalog, enabled task IDs, and world bank IDs. `mode` selects diagnostic or collection; `gamemode` and operator permissions belong to each scenario assignment. `worker_env` can explicitly configure rendering threads and settle/warmup budgets; selected settings are recorded in run metadata.

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

Workers stream raw captures, indexes, logs, heartbeats, resource samples, screenshots, outcomes, and metadata to the spool (`artifacts_dir`). A finalized run becomes immutable when its checksummed manifest is published last. The independent delivery queue verifies the spool, copies into a hidden central staging directory, verifies again, and atomically publishes a complete central run. Durable receipts and retries survive controller restarts. An interrupted or mismatched copy cannot become an admitted dataset entry.

`central_dir` is independent of disposable workers and SQLite control state. A filesystem mount is the selected backend; it can be a shared storage mount. Object-store credentials and a specific external object-storage service are not assumed. The store bounds live spool and central space and inhibits new starts when buffering/delivery cannot keep up. Already-running workers continue to be supervised.

Local spool retention, mutable-world retention, ordinary diagnostic retention, and longer failure retention are separate settings. Cleanup protects active/undelivered/unverified evidence, rejects symlink/path escapes, and verifies central identity before deleting a redundant local copy. Eligible training entries are never automatically expired by diagnostic retention. Runtime containers/Pods also have a bounded retention count and require verified local or central evidence before cleanup.

SIGTERM stops the client, drains the proxy, sends the server's console `stop`, and saves the world before finalization. Failed diagnostics can include a bounded world archive: regular files only, explicit logical/output/time limits, and a recorded reason when skipped. SIGKILL cannot run this cleanup. The controller preserves available partial files and publishes an invalid forensic result only after the runtime is terminated; Kubernetes API absence is treated conservatively with reserved capacity, stale-heartbeat waits, and artifact quiescence. Missing-process inference is labelled inferred, not confirmed task failure.

## Coverage and quality

Coverage counts include both observable presence and explicitly missing interaction facets. Inventory presence is not acquisition; looking at a block is not interacting with it; a nearby mob is not a successful fight. Server-confirmed controlled actions credit only their specific targets. Requested but unsupported targets stay visible. Failures and cooldowns prevent one repeatedly failing task from consuming the fleet indefinitely.

A run can provide useful diagnostic observations while remaining excluded from training. Final admission independently checks artifact integrity, actual task outcome, mode/permissions, client and server capacity samples, software/world identities, complete capture indexes, and watchdog/experimental flags. Long-task timeouts, cancelled tasks, inferred behavior failures, resource overload, missing server telemetry, and unknown outcomes are not successes.

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

# Worker telemetry, modes, and diagnostic evidence

The dedicated server uses the separately built `Vibrato/conductor-telemetry` Fabric mod. It observes real server tick boundaries and writes snapshots off the tick thread using a single pending snapshot. Minecraft, Fabric loader, mappings, and API versions come from Vibrato's existing Gradle properties. The observer also applies an explicitly supplied per-run default game mode before players join; enabling telemetry alone outside the worker does not change a server's game mode.

## Starting conditions

Workers default to Survival and a non-operator offline identity. `GAMEMODE` selects Survival, Creative, Adventure, or Spectator. `force-gamemode=true` plus the explicit server bootstrap overrides a pregenerated world's mode in the worker's private copy. Templates stay read-only. Creative terrain pregeneration therefore does not require Creative training workers.

`OPERATOR=true` is explicit and writes one `ops.json` entry for that run's deterministic offline UUID. `OP_PERMISSION_LEVEL` defaults to 2, accepts integer levels 1–4 for an operator, and grants nothing when `OPERATOR=false`. The normal catalog uses non-operators or level-2 operators; it never gives every client operator privileges. The metadata records `assignedGamemode`, `operator`, `operatorLevel`, identity, starting properties, source versions, and world revision.

Client heartbeats report `actualMode`, ability flags, and permission level. When an independently produced server snapshot matches that player's UUID, `permissionSource` is `server_observer`; otherwise the client-synchronized value is labeled `client_synced`. A confirmed mismatch between assignment and server/client mode or permissions stops the worker. Collection admission also requires a verified assignment context.

## Controlled scenarios

`@conductor_scenario NAME` performs bounded real client actions and checks the independent server observer. Scenario completion is separate from data-quality admission; a short successful scenario can still fail the timing quality gate.

| Name | Assigned mode | Operator | Goal evidence |
| --- | --- | --- | --- |
| `creative_inventory` | Creative | No | Creative inventory action gives a stone stack, then a flight update and ascent; server inventory, abilities, and position confirm both |
| `creative_build` | Creative | No | Normal hand/block interaction places stone at a reachable supported position; server-observed block changes from air to stone |
| `adventure_inspect` | Adventure | No | A block-dig attempt is rejected; the targeted solid block stays unchanged and server abilities prohibit world modification |
| `spectator_explore` | Spectator | No | Actual descending flight passes through solid ground without changing it; server position, flight, and collision state confirm it |
| `operator_commands` | Survival | Level 2 | `/time add 1200` executes; server world time confirms the requested change |

A scenario writes `scenario-result.json` with its name, status, `source=client_actions_plus_server_observer`, and structured evidence. `evidence.serverConfirmed=true` is written only after the goal predicate succeeds. Creative inventory is not labeled resource acquisition. Denied Adventure attempts are not labeled successful block interactions.

## Tick health and overload

The client writes cumulative `clientTicks` and monotonic elapsed `seconds`; the server writes cumulative `ticks` and `monotonicSeconds`. Worker rates use the change in ticks divided by the elapsed interval between distinct samples. The rolling window is the sum of ticks divided by the sum of elapsed time across ten intervals. This avoids aliasing one-second snapshots when the supervisor samples every two seconds or the game performs catch-up ticks.

Raw one-second `clientTps` and `serverTps` remain visible for inspection. `clientMeanTps`, `serverMeanTps`, and their sample counts are the weighted steady-state window. `clientIntervalTps` and `serverIntervalTps` expose the latest counter interval. The rate method is recorded as `cumulative_counter_delta_weighted_elapsed` in health and final quality summaries.

Terrain-loading heartbeats are liveness evidence, not proof that the game is ready. `worldReady` follows the script harness's first unobstructed world tick. The first `WARMUP_SECONDS=20` after that are retained in raw logs but excluded from steady capacity/overload measurements. `capacityReady` requires at least ten measured intervals for both processes. Explicit collection tasks use a `COLLECTION_SETTLE_SECONDS=45` post-completion window by default so fast successful tasks can accumulate valid timing evidence.

Resource samples include the whole worker's cgroup CPU usage, CPU throttling and pressure, memory use and limits, memory pressure, and OOM kills. Measured tick overload takes precedence over conclusions about bot behavior. A resource profile that works for one short task does not establish fleet capacity for every workload.

The software-rendering environment is recorded, including `LP_NUM_THREADS` when explicitly set, alongside requested CPU information and the actual cgroup CPU quota. Mesa documents this as a rendering-thread count control: [Mesa environment variables](https://docs.mesa3d.org/envvars.html). It does not require lowering game simulation, view distance, or FPS settings. Compare profiles with real timing data before changing a deployment default.

## Behavior and visual evidence

Heartbeats include task roots and leaves, true lifecycle start counts, the most recent starts, task outcomes, inventory counts, dimension, experience, block-break counters, current movement, and pillar attempts. Content observations distinguish inventory presence, crosshair-targeted blocks, and nearby loaded mobs. Movement alone is not counted as task progress; novel chunks count for explicit exploration contexts.

Watchdogs combine this evidence to identify repeated target restarts, small-area movement loops, failed pillar attempts without grounded height gain, unexpected idle state, and a long semantic-progress stall. These are labeled inferred unless the task explicitly reports a failure. Long progression tasks retain their assigned budgets. Cancellation, task timeout, command error, uncertain completion, and infrastructure/overload failures retain distinct result reasons.

Real framebuffer screenshots rotate between two client files. The supervisor copies only a completed image into `screenshot.png`; health records `screenshotWall` and age. `SCREENSHOT_SECONDS` defaults to 15 and accepts 5–300. Screenshots can be disabled explicitly with `SCREENSHOTS=false`.

## Failure-world snapshots

After stopping the client, flushing the recorder, and stopping the server, a failed worker can retain `world-snapshot.tar.gz` before publishing its manifest. The archive contains regular files from the private world only. Symlinks and special files are rejected. Defaults are 256 MiB logical/output bytes, 20,000 files, a ten-second checked processing budget, and gzip level 1. The output also respects remaining worker storage allowance. `SNAPSHOT_FAILURES=false`, `SNAPSHOT_MAX_BYTES`, and `SNAPSHOT_SECONDS` configure this behavior.

The result explicitly reports capture or a skipped reason and whether the server exited cleanly. An unclean snapshot is diagnostic evidence, not a consistent save guarantee. SIGKILL cannot run shutdown/archive code; available packet indexes, heartbeat history, logs, and incomplete markers remain the evidence. A filesystem blocked inside a kernel I/O call still needs the backend's overall worker deadline.

All final health, screenshots, snapshots, results, and server observations are written before the checksummed manifest. No artifact inside the finalized run changes afterwards.

## Live probes

`scripts/test-worker-live.py` launches exactly one real worker and verifies the final manifest, periodic PNGs, capture flushing, and measured tick health. Its `eligible` expectation additionally requires a proven task result and independent artifact quality admission. Fresh run IDs preserve previous evidence. Explicit EULA and offline-account flags are required.

```bash
python3 scripts/test-worker-live.py --id baseline-new --seconds 60 \
  --cpus 8 --render-threads 2 --image vibrato-worker:modes \
  --accept-eula --accept-offline
python3 scripts/test-worker-live.py --id creative-new \
  --task '@conductor_scenario creative_inventory' --gamemode creative \
  --mode collection --expect eligible --seconds 90 \
  --cpus 8 --render-threads 2 --image vibrato-worker:modes \
  --accept-eula --accept-offline
```

Explicit collection tasks expose `scriptPhase=post_task_settle` only when the assigned command and task outcome completed, its `wait-idle` barrier succeeded, and the final delayed EXIT is pending with no active task/path. `settleRemainingSeconds` and `settleBudgetSeconds` describe that bounded window (`COLLECTION_SETTLE_SECONDS`, default45, allowed0..3600). The watchdog matches the configured command/budget and maintains its own elapsed bound. It pauses behavior/idle classifiers during this verified window; tick overload, heartbeat/capture/process health, storage and lifetime limits remain enforced. Director runs and failed, cancelled or unconfirmed commands receive no settle exemption.

JVM worker pools are sized independently of CPU quotas. Both server and client receive `-XX:ActiveProcessorCount=8`, `-Djava.util.concurrent.ForkJoinPool.common.parallelism=8`, `-Djava.util.concurrent.ForkJoinPool.common.maximumSpares=64`, `-XX:ParallelGCThreads=4` and `-XX:ConcGCThreads=2` by default. `JVM_ACTIVE_PROCESSORS` (integer1..64) changes the processor sizing hint/common parallelism; GC thread counts are capped at4/2. ForkJoin compensation has a separate64 spare-worker ceiling instead of the JVM default256, leaving PID headroom for both JVMs and other processes. This bounds other JVM/Minecraft processor-sized pools on hosts with many cores while keeping the configured CPU/RAM quotas, gameplay and rendering settings intact. Exact flags are recorded in `metadata.jvmSizing`.

Resource histories/health include `pidsCurrent`, `pidsMax` (`null` means unlimited/unavailable), `pidsLimitEvents` and sticky bounded `nativeThreadFailure` log evidence. Explicit JVM native-thread OOME or pthread EAGAIN is classified `worker_native_thread_exhausted`, confirmed infrastructure, before generic tick overload and even during startup/warmup/intentional settle. It excludes collection admission even if the command later recovers. A cgroup OOM-kill counter of zero does not rule out native-thread exhaustion. Logs are checked incrementally with overlap and a64KiB per-process/poll bound.

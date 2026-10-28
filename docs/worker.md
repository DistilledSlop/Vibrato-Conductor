# Worker runtime

Each isolated worker runs Xvfb/Mesa, the real Vibrato client, a lossless recording proxy, and its own Fabric server. It needs a virtual OpenGL display, but no physical monitor or video recording. Periodic PNG frames support the dashboard gallery. See [operations](operations.md) for fleet configuration and [telemetry](worker-telemetry.md) for measured fields and scenario commands.

## Build and identity

Build the sibling Vibrato project, including `conductor-telemetry`, before running `scripts/build-worker.py`. The builder stages an explicit allowlist; saves, credentials, captures, and Git directories never enter the build context. Build metadata records source commits, dirty status, every staged input hash, and the client/server-observer JARs. Record the Docker image ID alongside source identity.

Minecraft/Fabric versions come from Vibrato's `gradle.properties`. Runtime dependency versions are pinned in the installer and proxy lockfile. Base image tags can move; the built image ID identifies the tested runtime. Building does not accept the EULA. Launches require explicit EULA/offline acceptance.

The client connects to the proxy on loopback port 25566, which forwards to its private server on 25565. Offline usernames are deterministic and limited to 16 characters. Every run records its full identity and seed. The server observer supplies authoritative ticks, mode, permissions, inventory, and controlled action evidence. Ordinary Survival assignments are non-operator; other modes and operator level require explicit scenario assignments. Optimization mods are optional experimental profiles and remain outside ordinary collection admission.

## Worlds and storage

Every run ID is exclusive. Runtime and artifact directories cannot be reused. The bank is mounted read-only; each worker receives a private writable clone. Cloning prefers reflinks and requires explicit permission for full-copy fallback. Full content hashes identify the source and clone, with before/after checks against mutation. Sidecar metadata keeps the exact source seed and original mode separate from the assignment.

Artifacts stream directly to a persistent spool independent of the disposable worker. Finalized manifests are published last and then remain immutable. The fleet delivery queue independently verifies and atomically publishes complete runs into `central_dir`, with durable retries, buffering limits, and protected retention. The selected backend is a filesystem mount; external object storage is not configured. See [operations](operations.md#artifact-delivery-and-retention).

Artifacts include metadata and input hashes, separate process logs, client/server heartbeats, cgroup resources, raw packet payloads and indexes, recorder flush markers, periodic screenshots, task/scenario outcomes, result, and checksummed manifest. Failed runs can also retain a bounded world archive. Use `python3 -m conductor verify PATH` to check integrity; integrity alone does not establish training eligibility.

The proxy bounds queued bytes and raw session size and fails closed on recording errors. Worker disk budgets are sampled watchdog thresholds, rather than filesystem quotas. Docker/Kubernetes enforce CPU and memory limits. The tested profile uses 8 CPUs, 6 GiB, and `LP_NUM_THREADS=2`; workload and concurrency still require measured validation.

## Health, outcomes, and shutdown

Health requires game readiness, a warmup interval, fresh client/server heartbeats and capture evidence, and sufficient cumulative tick samples. Weighted counter deltas avoid sampling aliasing. Both client and server must sustain the admission threshold; process liveness alone cannot establish capacity. Raw loading and warmup samples remain available for diagnosis.

Task success requires a completed assigned command and matching non-cancelled outcome. Long progression tasks additionally need their actual goal evidence. Capacity, exact mode/permissions, complete captures, software/world identity, and watchdog status are independently checked before collection admission. Failed, unknown, cancelled, overloaded, or experimental runs remain diagnostic evidence.

The watchdog distinguishes timing/resource pressure from semantic task stalls, bounded movement loops, repeated crafting restarts, and repeated failed pillaring. Mere stillness or navigation restarts do not prove failure. Inferred classifications remain labelled as inferred.

SIGTERM stops the client, drains the proxy, sends server console `stop`, waits for the world save, and stops the display before publishing final artifacts. Stalled processes receive bounded signal escalation. SIGKILL cannot finalize evidence; the controller conservatively quarantines retained partial data after termination. Containers/Pods are cleaned only under owned scope and the configured verified-evidence retention policy. Controller restarts preserve workers for adoption.

## Checks

```bash
python3 -m unittest discover -s tests -v
python3 scripts/test-worker-live.py --id fresh-probe-001 --task '@get dirt 1' \
  --seconds 90 --mode collection --expect eligible --cpus 8 \
  --accept-eula --accept-offline
```

Use fresh run IDs and inspect the full result and manifest. A duration-bound autonomous diagnostic is not proof that its long-term goal completed. The [mode validation report](mode-validation-2026-10-27.md) records actual Creative, Survival, Adventure, Spectator, and operator results.

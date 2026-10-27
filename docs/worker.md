# Single-worker runtime

## Build and identity

`scripts/build-worker.py` stages an allowlist of inputs into `.state/build-context` before building `vibrato-worker:dev`. It never sends sibling saves, credentials, captures, or Git directories to Docker. Build metadata records commits, dirty status, SHA-256 for every staged input, and the exact built Vibrato jar. Rebuild the mod before building the worker after source changes.

Minecraft, Fabric loader, and Fabric API versions come from Vibrato's `gradle.properties`. Fabric installer is pinned to 1.0.3, minecraft-launcher-lib to 8.0, and npm dependencies use the sibling proxy's lockfile. Ubuntu 24.04 and Node 22 base tags track their maintained images; record `docker image inspect` output for exact image identity. The installation step does not accept the EULA. Runtime requires explicit EULA and offline-mode flags.

The client always gets `vibrato.server=127.0.0.1:25566`; the recording proxy forwards to `127.0.0.1:25565`. Both listeners bind loopback inside the worker's namespace. Each client uses a deterministic offline username of at most 16 characters, with the full run identity and seed recorded separately. No fixture mods, operators, altered game rules, or optimization mods are installed. New worlds use normal survival settings.

## Storage

A run id is exclusive. Reusing an existing workspace or artifact directory fails. Runs write isolated mutable server state below `.state/workspaces/ID/ID/`. The duplicate final ID is the worker's internal namespace below its mounted workspace.

Artifacts go to `.state/artifacts/ID/` by default. `--artifacts /mount/dataset` can point to a persistent shared filesystem. The artifact mount is independent of the disposable worker filesystem. Captures and logs stream directly into it; there is no upload step for this backend. Object storage, network outage retries, rotation into upload segments, and retention management are not yet implemented.

Retained artifacts include:

- `metadata.json`: assignment, seed, world identity, server properties, connection path, software versions, source revisions, and input hashes.
- `logs/`: separate client, proxy, Fabric server, and Xvfb output.
- `heartbeats.jsonl` and `heartbeat.json`: sampled client task leaf, position, dimension, health, deaths, and client tick cadence.
- `resources.jsonl`: cgroup CPU, memory, memory events, and capture bytes. These include the display and server as well as the client.
- `captures/`: original `.bin` payloads and reader-compatible `.jsonl` indexes, plus per-session `.status.json` flush markers.
- `script-result.json`, `result.json`, and `manifest.json`: script results, worker classification, checksums, and a final artifact inventory.

A recorder marker is incomplete until its streams close successfully. A missing run manifest means finalization was interrupted. Verify completed artifacts with `python3 -m conductor verify PATH`. A valid manifest establishes file integrity, not training eligibility. `datasetEligible` is always false in this milestone, and `taskSuccess` remains unknown.

The proxy caps queued recording data at 8 MiB and raw bytes per session at 1 GiB. Exceeding either fails the session and proxy instead of silently dropping captured data. The worker bounds total mutable file usage at 4 GiB by default, sampled every two seconds; this is a watchdog threshold, not a filesystem quota. Logs and world growth can overshoot between samples. Docker limits CPU, memory, PIDs, and shared memory; temporary display space has its own bound. `RUN_SECONDS` is a task budget, and startup has a separate deadline. The CLI stops containers with up to 90 seconds for ordered cleanup.

## World bank

Use this bank layout, mounted read-only:

```text
bank/
  forest-001/
    world/
      level.dat
      region/
      ...
```

```bash
python3 -m conductor run --id forest-probe-001 --seconds 120 \
  --world-bank /mount/bank --world-id forest-001 \
  --accept-eula --accept-offline --wait
```

World directories must have no symlinks or special files. Workers try `cp --reflink=always` and never write to the bank. Unsupported copy-on-write storage fails unless `--allow-world-copy` is passed. The metadata records copy mode, logical byte size, and the source `level.dat` hash. Full copies consume the whole world's logical size; they are not shared writable worlds. The bank must stay immutable throughout copying. A bank revision manifest covering every world file remains to be added; a `level.dat` hash alone does not identify every chunk.

## Monitoring and cleanup

```bash
python3 -m conductor status
python3 -m conductor stop vibrato-forest-probe-001
```

The stop command checks the conductor ownership label. Containers remain after exit for inspection. Once evidence has been verified and retained, remove an exited container with `docker rm NAME` and remove its workspace separately when no longer needed. The CLI never deletes bank worlds or artifacts. No background reconciliation or automatic replacements run yet.

Docker health requires a recent supervisor update, client heartbeat, and capture growth. Process failures, missing heartbeats, capture stalls, exhausted startup/lifetime budgets, and local storage thresholds retain an explicit reason. SIGTERM stops the client, flushes the proxy, sends the server console `stop` command to save the world, and stops Xvfb. A stalled server then receives SIGTERM, followed by SIGKILL only after a further timeout. A passed diagnostic requires the client, proxy, and server to exit zero. SIGKILL cannot finalize artifacts; partial captures retain incomplete markers. The supervisor owns only its own process groups.

Dedicated-server tick telemetry is currently absent: the existing client heartbeat reports null `serverTps` on multiplayer. Client tick cadence and cgroup samples cannot establish server capacity. No motion-only stall classifier is enabled. Nerd-pole failures, task loops, and semantic progress require additional instrumentation before automatic behavior diagnosis or training collection.

## Tests

```bash
python3 -m unittest discover -s tests -v
# Host Node is optional; run the recorder's focused tests with Docker:
docker run --rm --network none \
  -v /opt/Protocol-Transformer/proxy:/proxy:ro -w /proxy \
  node:22-bookworm-slim node --test test/recorder.test.js
```

Use a fresh run id for every real smoke test. Inspect `result.json`, verify the artifact manifest, and confirm both C2S and S2C play packet observations. A duration-bound autonomous run tests the worker path and is not evidence that a long task succeeded.

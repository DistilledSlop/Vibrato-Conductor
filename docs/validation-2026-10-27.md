# Worker validation — October 27, 2026

Validated the first Docker worker milestone against a real Minecraft 1.21.1 client, Vibrato, the recording proxy, and a separate Fabric server. All runs were single-worker diagnostics with 4 CPUs, a 6 GiB container limit, baseline rendering, and no optimization mods. These limits have not been established as sustainable fleet sizing.

## Live runs

| Run id | Assignment budget | Heartbeats | Captured packets | Raw bytes | Outcome |
| --- | --- | --- | --- | --- | --- |
| `smoke-20261027-01` | 45 s | 3 | 5,807 | 2,548,552 | Operator stop; correctly retained as failed diagnostic, capture flushed |
| `smoke-20261027-02` | 45 s | 24 | 38,692 | 11,692,896 | Autonomous worker diagnostic passed |
| `bank-20261027-01` | 15 s | 9 | 12,377 | 4,062,177 | Bank world loaded and diagnostic passed |
| `bank-20261027-02` | 15 s | 8 | 12,900 | 4,084,751 | Final runtime passed; client, proxy, server, and display all exited zero |

Budgets start after the client enters the world; whole-worker runtimes include installation-independent server/client startup and shutdown. The final run took about 73 seconds end to end. A passed diagnostic does not establish success for a progression task; these runs used the seeded autonomous director. Task success remains unknown and every run has `datasetEligible=false`.

The intermediate builds signaled the server during shutdown. The final runtime sends the server console `stop` command first, with signals reserved for a timeout. The final run's log confirms player saving, world saving, and all dimensions saved before exit. A passing diagnostic now also requires client, proxy, and server exit code zero.

The first run exposed native-library extraction into the shared installation. Each client now receives a writable native directory inside its workspace. The server's bundler also receives writable per-run libraries. The final image input hashes were compared with the current source files and all matched.

## Capture and storage evidence

- All four finalized artifact manifests passed size and SHA-256 verification.
- The existing Protocol Transformer reader round-tripped 5,771 play packets from the interrupted run, 38,656 from the autonomous smoke, and 12,864 from the final bank run. All were byte-identical after parsing and encoding, with no parse errors. This check covers play packets, not every protocol phase.
- The final run observed both directions: 179 C2S play packets and 12,685 S2C play packets.
- The world bank's 17 file hashes were unchanged after both world-bank runs. Workers mounted it read-only and used separate mutable copies.
- The bank contained 5,861,891 logical bytes. This filesystem required full copies; those runs explicitly enabled the fallback. Snapshot/reflink efficiency has not been validated on another filesystem.
- Logs, metadata, heartbeats, resources, captures, results, and manifests are retained below `.state/artifacts/RUN_ID/`. Mutable worlds remain below `.state/workspaces/`. These are ignored by Git.

## Automated checks

- Six conductor tests passed: configuration validation, packet/index integrity, orphan capture detection, immutable world copying and fallback, manifest corruption detection, and server console shutdown.
- Four recorder tests passed: byte/index offsets and idempotent close, bounded queued bytes, bounded capture bytes, and file-open failure handling.
- Eighteen existing reader/parser tests passed against the installed proxy dependencies.
- Vibrato's `./gradlew test build -PbaritoneDist=/opt/Baritone/dist` succeeded with its tasks up to date; no Java source was changed.
- Python compilation and Git whitespace checks passed.

No worker containers were left running after these tests. Exited test containers remain available for inspection.

## Remaining work

This validates the single-worker connection and artifact path. It does not validate dedicated-server tick health, semantic task progress, loop or nerd-pole diagnosis, long-task success, optimization-mod equivalence, or multi-worker capacity. Dedicated-server `serverTps` is still null in the existing client heartbeat. Do not promote these diagnostics into the training dataset.

Next implementation work follows the [requirements](requirements.md): strengthen world revisions and storage delivery/retention, integrate server and task telemetry with behavior watchdogs, then add fleet reconciliation, Kubernetes scheduling, dashboard, and coverage-driven assignments before capacity and training runs.

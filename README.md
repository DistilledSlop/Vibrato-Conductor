# Vibrato Conductor

Runs Minecraft 1.21.1 clients with Vibrato through the Protocol Transformer recording proxy, each against its own Fabric server.

The first implementation provides a single Docker worker, a local CLI, isolated world-bank copies, and durable filesystem artifacts. It includes Xvfb/Mesa, process supervision, client heartbeats, bounded captures, shutdown flushing, and checksummed run manifests. Every run is diagnostic and excluded from training by default. Fleet reconciliation, Kubernetes scheduling, dashboard, authoritative behavior watchdogs, remote uploads, and coverage scheduling remain planned in [the requirements](docs/requirements.md).

Requires Docker, Python 3.11+, and the sibling `Vibrato`, `Protocol-Transformer`, and patched `Baritone` repositories. No Node or Python packages need installing on the host. Build/download steps need internet; worker runs use an isolated network namespace with no external connectivity or published game ports.

```bash
cd /opt/Vibrato
./gradlew test build -PbaritoneDist=/opt/Baritone/dist
cd /opt/Vibrato-Conductor
python3 scripts/build-worker.py

# Read and accept the Minecraft EULA before using --accept-eula.
python3 -m conductor run --id smoke-001 --seconds 60 \
  --accept-eula --accept-offline --wait
python3 -m conductor status
python3 -m conductor verify .state/artifacts/smoke-001
```

Default limits are 4 CPUs and 6 GiB for the combined worker; these are initial smoke-test limits, not measured fleet sizing. Set `--cpus`, `--memory`, and `--seconds` explicitly for experiments. Baseline rendering uses the normal Vibrato profile; `--perf low` explicitly enables the existing rendering reductions and records that choice. No optimization mods are added.

`--task '@nether'`, `--task '@gamer'`, or another single Vibrato command creates a bounded scripted diagnostic assignment. The default `director` runs seeded autonomous activity. A passed worker diagnostic proves the connection, telemetry, script exit, and flushed capture structure; it does not prove task success, coverage completeness, or sustainable capacity.

Real single-worker diagnostics passed; see [the validation record](docs/validation-2026-10-27.md).

See [worker operation](docs/worker.md) for storage, world banks, failure handling, testing, and remaining limitations.

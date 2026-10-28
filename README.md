# Vibrato Conductor

A persistent fleet controller for Minecraft 1.21.1 data collection. Each worker runs a real Vibrato client → recording proxy → its own Fabric server. Worlds are isolated copies of an immutable bank; captures and diagnostic evidence survive worker replacement.

The controller provides Docker and Kubernetes backends, restart recovery, paced resource-limited launches, crash/unhealthy replacement, failure backoff, coverage-driven assignments, verified artifact delivery, and an authenticated monitoring dashboard. The gallery updates visible worker screenshots every 15 seconds; open a card for a larger screen, task state, tick health, process logs, and retained evidence.

## Run locally

Requires Docker and Python 3.11+. Building Vibrato requires Java 21 and the sibling patched Baritone checkout. Game/runtime dependencies live inside the images.

```bash
cd /opt/Vibrato
./gradlew test build -PbaritoneDist=/opt/Baritone/dist
cd /opt/Vibrato-Conductor
python3 scripts/build-worker.py
python3 scripts/build-controller.py

cp deploy/fleet.docker.example.json .state/fleet-local.json
# Review paths, resource budgets, world bank, and enabled tasks in that file.
# Set accept_eula/accept_offline only after accepting those operating conditions.
python3 -m conductor serve --config .state/fleet-local.json --port 8080
```

The dashboard binds **0.0.0.0:8080** by default. Open **http://HOST:8080/** using the server’s reachable address (or **http://127.0.0.1:8080/** on the server). By default, read the access-token file printed on startup and paste it into the console. Set `"require_token": false` in the service configuration to open the dashboard without a token. The initial fleet target is zero; use the dashboard to start workers. Keep the token out of Git and chat. An operator-managed TLS ingress or authenticated tunnel can also front the dashboard.

The starting headless profile uses an explicit CPU/memory limit and software rendering thread budget. Size against **measured client and server tick health**, including rendering, server, proxy, and supervisor costs. A process being alive is insufficient. The shipped worker ceiling is deliberately one; raise it only after running capacity tests on the deployment host.

```bash
python3 -m conductor scale 1 --token-file .state/fleet/dashboard.token
python3 -m conductor fleet-status --token-file .state/fleet/dashboard.token
```

Use **Retry after fix** to clear a recorded failure backoff once its cause has been addressed. Resource, storage, acceptance, and desired-count limits still apply. Stop reduces the target; Replace preserves it and creates a fresh run/world copy.

## World bank

The 50 supplied saves have been registered at `/opt/world-bank/world1/world` through `/opt/world-bank/world50/world`. All are Minecraft 1.21.1. Their complete content fingerprints, exact source seeds, original gamemodes, stored chunk bounds, and origins are recorded in each entry's `world.json`; the import report is `.state/world-import/report.json`. No second full copy was made.

```text
/opt/world-bank/
  world1/
    world.json
    world/
      level.dat
      region/
      ...
```

Creative pregeneration is supported. Each private server sets its assigned mode, enforces it for joining players, and grants operator privileges only when the assignment explicitly requires them. A template's Creative mode or integrated-server cheats flag does not grant the collection bot operator access. Actual mode and permission levels are verified from the server observer.

Configure `world_bank`, `world_ids`, and `allow_world_copy`. The bank is mounted read-only. Copies prefer reflinks; unsupported filesystems require explicit full-copy fallback with recorded disk cost. Region-header bounds describe saved chunks; they do not certify a particular complete pregeneration radius.

Register additional stopped saves with:

```bash
python3 scripts/import-world-bank.py /path/to/saves /opt/world-bank --move
```

The move option requires the same filesystem; `--copy` explicitly permits a full copy. Existing bank entries are never overwritten. Do not open bank templates for play or modify them while workers use them; edit a separate copy and import it as a new revision.

## Tasks, modes, and data

Assignments separate gamemode from permissions. Ordinary gathering/progression is Survival without OP. Controlled Creative inventory/build/flying, Adventure restrictions, Spectator pass-through, and operator-command scenarios exercise distinct behavior. Exact task definitions, budgets, prerequisites, and evidence rules live in [catalogs](catalogs/README.md).

A single explicit scenario can be run without the fleet:

```bash
python3 -m conductor run --id creative-probe-001 --seconds 90 \
  --task '@conductor_scenario creative_inventory' --gamemode creative \
  --cpus 8 --render-threads 2 --accept-eula --accept-offline --wait
```

The inventory separates requested targets, observed evidence, task success, missing targets, and unsupported interactions. Scheduling a task never proves coverage. Raw protocol IDs are scoped by phase and direction. Model input rows also retain actual initial gamemode, abilities, and server permission context. Rebuild preprocessing/vocabulary when adopting the updated reader; old checkpoints do not gain those new context tokens automatically.

Every run is diagnostic by default. Training admission additionally requires explicit collection mode, confirmed task success, matched actual mode/permission context, complete captures, valid cumulative tick measurements, verified software/world identity, and no invalidating watchdog classification. Experimental optimization profiles remain excluded pending behavior review.

## Deployment and checks

See [deployment](deploy/README.md), [worker operation](docs/worker.md), [operator guide](docs/operations.md), [performance experiments](docs/performance.md), and the [requirements](docs/requirements.md).

```bash
python3 -m unittest discover -s tests -v
python3 scripts/test-cluster.py up
python3 scripts/test-cluster.py load --image vibrato-worker:dev --image vibrato-controller:dev
```

Live test scripts create real game workers and retain their evidence under `.state/`; use a separate test fleet/configuration. Raw saves, credentials, captures, and local test state are excluded from Git. The original [single-worker validation](docs/validation-2026-10-27.md) is historical; the [fleet validation report](docs/fleet-validation-2026-10-28.md) and [mode validation report](docs/mode-validation-2026-10-27.md) record the later results and limits.

The later [task-diversity and production failure audit](docs/task-diversity-and-failure-audit-2026-10-28.md) records the expanded task mix, native-thread exhaustion repair, and independent admission revocations.

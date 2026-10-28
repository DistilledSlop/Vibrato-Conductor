> Historical requirements and initial handoff snapshot. Statements about missing implementation or tests describe the starting state, not current status. See [README](../README.md), [operations](operations.md), and the validation reports for implemented behavior and measured limits.

# Vibrato Conductor requirements

Recorded on October 27, 2026, from the project discussion. This describes the system to build, rather than an implemented system.

## Purpose

Build a Docker and Kubernetes system that runs many independent Minecraft 1.21.1 clients using Vibrato. Each client must join its own running server through the Protocol Transformer recording proxy, perform varied tasks, and produce centrally stored packet captures for a training dataset.

The conductor must manage the fleet, provide a dashboard for manual monitoring, automatically detect unhealthy or unproductive workers, and maintain a configured number of active agents within the available CPU and memory. Collection must deliberately pursue broad coverage: a model cannot learn behavior or content that never appears in its training data.

## Projects involved

| Project | Location | Role |
| --- | --- | --- |
| Vibrato Conductor | `/opt/Vibrato-Conductor` | Fleet orchestration, dashboard, task scheduling, diagnostics, and dataset collection management |
| Vibrato | `/opt/Vibrato` | Minecraft client mod, autonomous tasks, and client telemetry |
| Protocol Transformer | `/opt/Protocol-Transformer` | Recording proxy, packet reader and transformer, and training pipeline |
| Baritone | `/opt/Baritone` | Patched pathfinding dependency used by Vibrato |

Changes to Vibrato and Protocol Transformer are part of the intended scope whenever needed to make the complete system work. The user explicitly requested direct integration with those projects. Protocol Transformer is currently not in Git and needs cleanup and repository setup while preserving its working functionality. The conductor repository also needs setup.

## Worker runtime

Each worker container must contain:

- A real Minecraft 1.21.1 client running the Vibrato Fabric mod.
- The Protocol Transformer proxy that forwards traffic and records packets.
- A Minecraft 1.21.1 Fabric server hosting an isolated world for that client.
- A supervisor that starts the components, checks their health, gathers evidence, and ensures captures reach central storage.

The required connection path is client → recording proxy → server. A client that bypasses the proxy does not fulfill the collection requirement. Keep packet captures and diagnostic artifacts associated with their worker, world, task, and run.

Support unattended operation on headless hosts. The existing client requires OpenGL; its fleet documentation describes Xvfb with Mesa software rendering as an option. The runtime must account for that CPU cost when determining capacity.

If a worker crashes, disconnects, becomes stuck, or otherwise stops producing useful data, preserve the available evidence and replace it with a fresh worker. Replacement must not repeatedly reuse the same broken mutable world state.

## World bank

Support a supplied bank of pregenerated worlds so workers do not repeatedly generate terrain or consume unnecessary disk space.

Treat the bank as immutable. Each running server needs isolated mutable state so simultaneous workers cannot corrupt one another's worlds. Prefer storage snapshots, reflinks, or another supported copy-on-write mechanism where available; the storage implementation still needs to be selected and tested on the deployment filesystem. Do not share one writable world directory between servers.

Track which world and starting conditions each run uses. World selection should support task requirements, varied environments, and reproducible diagnostic runs. A fallback that copies entire worlds should make its disk cost explicit.

## Fleet management and resource use

The conductor must:

- Maintain a configurable target number of running agents.
- Start, stop, replace, and inspect workers through Docker and Kubernetes.
- Account for the combined resource use of client, server, proxy, supervisor, and display runtime.
- Size and schedule workers against available CPU, RAM, and temporary storage.
- Stagger startup to avoid simultaneous memory and CPU spikes.
- Monitor resource pressure and avoid increasing concurrency beyond measured sustainable capacity.
- Recover fleet state after a conductor restart and avoid starting duplicate workers.
- Bound worker lifetime, local capture buffering, and diagnostic retention.

Use explicit Kubernetes resource requests and limits. Worker counts must reflect actual game tick health and capture throughput as well as whether containers are still running. Hardware overload must be distinguishable from a bot behavior failure.

## Performance without gameplay changes

Use Fabric for both server and client. Evaluate FerriteCore, Lithium, and other compatible optimizations to reduce CPU and memory consumption without changing Minecraft functionality.

Preserving behavior is a requirement, not an assumption based on an optimization mod's description. Pin versions compatible with Minecraft 1.21.1, record the mod set, and validate it against the baseline. Do not silently change game rules, simulation behavior, mob behavior, or task semantics to improve throughput. Rendering reductions must be assessed for their effect on any observations used for training.

The existing Vibrato fleet guide gives planning estimates of roughly 2–2.5 GB per client, plus server and display costs. These are unmeasured estimates and must not be treated as validated worker sizing. A complete worker here includes a separate server for each client.

## Tasks and dataset coverage

Workers must run varied, randomized tasks, with scheduling that intentionally fills coverage gaps rather than relying only on random wandering.

The dataset goal includes coverage across:

- Every required task and important task sequence, including long progression tasks.
- Items, including acquisition, use, and relevant inventory interactions.
- Blocks and relevant interactions or state changes.
- Mobs and relevant interactions or behavior.
- Protocol packets, distinguished by protocol state and direction.

Maintain a coverage inventory that separates requested targets from actual observations and successful task outcomes. Record missing and unsupported targets explicitly; executing a scenario does not prove that all intended packets or interactions occurred.

Use the inventory to assign underrepresented work and spread tasks across suitable worlds and starting conditions. Record seeds and assignments so failures and useful examples can be reproduced. The exact coverage targets and evidence rules must be defined during implementation using the projects' existing registries, manifests, and task capabilities.

Vibrato may need additional commands, task support, telemetry, or instrumentation to expose behavior missing from the training data. Protocol Transformer may need recording and transformation changes to retain and interpret that evidence.

## Central capture storage

Store captured packet data centrally rather than leaving the only copy in disposable worker filesystems. Preserve raw captures and the indexes needed by the existing reader, alongside run metadata and quality labels.

Design for interrupted workers and failed uploads: bounded buffering, retries, detectable incomplete captures, and a reliable distinction between uploaded artifacts and completed dataset entries. Associate records with the Minecraft version, mod and source versions, world identity, task assignment, and outcome. Keep diagnostic or invalid runs identifiable so they can be excluded from the main training dataset.

The central storage backend, upload mechanism, and retention settings remain implementation choices. They have not been selected yet.

## Monitoring dashboard

Provide a dashboard for manual inspection in addition to automated monitoring. It should expose:

- Desired, starting, healthy, unhealthy, and completed worker counts.
- Per-worker task, world, lifecycle state, runtime, and recent progress.
- CPU, memory, and game tick health where measurable.
- Capture activity, storage delivery status, and failures.
- Recent logs, failure reasons, and links to retained diagnostic evidence.
- Task and dataset coverage, including gaps and repeated failures.
- Controls to inspect, stop, or replace individual workers and adjust fleet size.

The dashboard must help an operator understand what each bot is doing. The exact mechanism for viewing a live client, such as streamed display access or screenshots, still needs to be selected.

## Diagnostic testing mode

Provide a separate testing phase before the main training run. This mode must closely monitor behavior and retain enough evidence to fix failures in Vibrato or its dependencies.

Specifically investigate:

- Bots that remain stuck or make no meaningful progress.
- Movement loops and repeated task restarts that masquerade as activity.
- Nerd pole failures, including repeated attempts without gaining height.
- Long tasks such as `@nether` and `@gamer` that stall, fail, or exceed a reasonable budget.
- Crashes, disconnects, recording failures, and hardware overload.

Do not use movement alone as proof of task progress or stillness alone as proof of failure. Some legitimate work takes place at one position, and loops can involve continuous movement. Combine task context, position history, outcomes, timing, and resource health. Give long tasks suitable budgets and preserve the reason a watchdog stopped them.

Retain structured heartbeats, recent task history, logs, position or dimension history, relevant inventory and task state where available, process exit information, resource samples, and capture references. Include seeds, world identity, and software versions for reproduction. Diagnostic output should distinguish confirmed outcomes from inferred watchdog classifications.

Testing should progress from a single worker to increasing concurrency. Establish sustainable capacity and repair recurrent behavior failures before using the fleet for the main training dataset. No in-game testing or capacity measurement has been completed in this session.

## Existing integration points found during inspection

Vibrato already includes useful starting points:

- `scripts/fleet/README.md` and `scripts/fleet/launch.py`: installation, offline client identities, automatic server connection, performance settings, and staggered launches. The guide explicitly says the launcher and fleet configuration are untested in-game.
- `scripts/campaign/README.md` and `scripts/campaign/supervisor.py`: isolated campaign runs, watchdogs, task churn detection, heartbeats, resource sampling, and overload classification. This harness currently runs singleplayer campaigns, rather than the required dedicated-server path through a proxy.
- `src/main/java/dev/vibrato/fleet/FleetBootstrap.java`: automatic world entry or server connection and autonomous director startup.
- `src/main/java/dev/vibrato/fleet/FleetConfig.java`: per-client configuration through `-Dvibrato.*` properties.
- `src/main/java/dev/vibrato/validation/ScriptRunner.java`: scripted client commands, task outcomes, timeline samples, and optional campaign heartbeats.
- `src/main/java/dev/vibrato/packetcoverage/`: packet observation and coverage reporting, with a protocol 767 manifest and explicit evidence requirements.
- `src/main/java/dev/vibrato/commands/ProtocolCommand.java`: protocol diagnostic commands and scenarios. Some scenarios require a local fixture; they must be reviewed before scheduling them on ordinary worker servers.

The current Vibrato build targets Minecraft `1.21.1`, Fabric loader `0.16.14`, Fabric API `0.116.7+1.21.1`, and Java 21. Recheck these properties when implementing instead of maintaining a conflicting copy of version settings.

Protocol Transformer already includes:

- `proxy/index.js`: a Minecraft 1.21.1 recording proxy with configurable proxy and target ports.
- `proxy/recorder.js`: raw `.bin` captures, `.jsonl` packet indexes, and optional parsed JSON logs. Raw bytes are recorded before forwarding.
- `record.sh`: starts recording with parsed logs disabled by default to reduce disk usage.
- `reader/`: packet reading, transformation, coverage tooling, and tests.
- `train/`: training code and tests.

During inspection, the proxy capture directory was fixed relative to its source directory, shutdown flushing handled SIGINT, and recorder writes used streams. Review configurable storage paths, SIGTERM handling, write errors, backpressure, and bounded buffering for container operation. These are integration review items; they have not been fixed or load-tested here.

The proxy terminates login on each leg rather than acting as a transparent byte tunnel for every phase. Its own comments describe this limitation. Verify what is actually captured before claiming complete login, handshake, status, configuration, or play coverage.

## Starting implementation sequence

1. Obtain write access to the sibling projects, inspect applicable repository instructions, and set up source control without discarding existing files or generated data.
2. Build one complete worker container and connect its real client through the recorder to its dedicated Fabric server.
3. Add isolated world-bank reuse, central artifact delivery, and reproducible run metadata.
4. Integrate client telemetry, task assignment, watchdogs, and diagnostic artifact collection.
5. Build conductor reconciliation, Kubernetes deployment configuration, and the monitoring dashboard.
6. Implement coverage inventory and scheduling for missing or underrepresented tasks and content.
7. Run single-worker behavior tests, fix failures, and then measure increasingly large fleets before starting the main collection run.

This sequence is an implementation starting point, not a completed architecture decision or a claim that any runtime component is already working.

## Original environment and handoff (historical)

Implementation resumed after this handoff. See [the README](../README.md) and [worker operation](worker.md) for the current implementation and validation status. The following records the original inspection only.

Only initial source inspection and temporary write-access checks were performed. No conductor runtime, deployment manifests, dashboard, integration patches, or source cleanup have been implemented. No sibling source files were changed. These requirements and the README are the first files created here.

The most recent access check found `/opt/Vibrato-Conductor` writable, while `/opt/Vibrato` and `/opt/Protocol-Transformer` returned `Read-only file system`. The user wants both sibling projects writable so direct integration can proceed. The session's sandbox escalation is disabled.

The installed Codex CLI supports resuming with additional writable directories:

```bash
codex resume --last \
  --cd /opt/Vibrato-Conductor \
  --sandbox workspace-write \
  --add-dir /opt/Vibrato \
  --add-dir /opt/Protocol-Transformer
```

The last recheck still showed read-only sibling paths. If restarting with these flags does not change access, the managed workspace permissions need adjustment. Git metadata in this workspace is also mounted read-only, and `git status` reported that this directory is not currently a Git repository; repository initialization remains unfinished.

Java 21 and Python 3 were available. Node.js and `kubectl` were not found on PATH during inspection. Docker was installed, but access to its daemon socket was denied. These conditions must be rechecked and resolved as needed before claiming container or cluster validation.

The user requested saving these requirements locally and then stopping. Resume implementation only when asked to continue.

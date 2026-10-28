# Task diversity and production failure audit — October 28, 2026

## Findings

The audited production cohort is the 33 finalized gallery runs beginning at `20261028003856`, after CPU/memory quotas were removed and the operator selected a larger target. Earlier deliberate crash/restart tests are excluded from this denominator.

| Original worker outcome | Runs |
| --- | ---: |
| Normal completion | 14 |
| Client tick overload | 10 |
| Server tick overload | 1 |
| Explicit Creative placement failure | 1 |
| Operator/scale-down stop | 7 |

Seven runs contain confirmed native-thread exhaustion. This overlaps the outcome categories above: a recovered native-thread error could previously be labelled normal completion. The JVM sized pools against the 96-CPU host, with common-pool worker numbers exceeding 150. A live affected run reached `pids.current=498`, `pids.max=512`, and 184 PID-limit events. Logs contained `OutOfMemoryError: unable to create native thread` and `pthread_create` EAGAIN. Zero cgroup OOM kills did not rule this out: it was thread/PID exhaustion, not proof of Java heap or host RAM exhaustion.

Native-thread evidence was found in:

- `20261028003856-63861e6f80`
- `20261028003952-77e5221bad`
- `20261028004049-64efa9cf1c`
- `20261028004228-9c5ad36c73`
- `20261028004251-7df6d7dee0`
- `20261028004310-c57217a17a`
- `20261028004329-bad0c2acf6`

The two recovered runs `64efa9cf1c` and `9c5ad36c73` had been incorrectly admitted. All 56 delivered gallery runs, including historical diagnostics, were re-audited. Both admissions were revoked. Current delivery decisions and central `.admission/RUN_ID.json` receipts exclude them; original manifests/results/logs remain byte-for-byte evidence. The API now exposes independent admission alongside the worker verdict, and the inspection view shows dataset exclusion reasons. Training consumers must use current independent admission, not the original worker flag alone.

The audited captures were complete; strict spool/central manifest checks did not identify recorder corruption. The Creative placement failure explicitly reported no reachable supported air block while the client stood in an acacia canopy. Its current placement predicate excludes leaves and searches a small local region. That terrain-specific scenario limitation remains; it was not a server or recorder crash and was excluded from training.

Other tick-overload failures are real quality failures requiring fresh workload/concurrency measurement. Fixing thread exhaustion does not establish that every workload can sustain a target of 15.

## Repair and rollout

Both JVMs now receive explicit pool-sizing hints: `ActiveProcessorCount=8`, common parallelism 8, common maximum spares 64, parallel GC threads 4, and concurrent GC threads 2. `JVM_ACTIVE_PROCESSORS` configures the sizing hint. These settings bound pool creation; Docker CPU and memory quotas remain absent. Rendering/gameplay/FPS/view distance were not reduced. PID 512 remains the ordinary runtime guard. Eleven existing affected-fleet workers temporarily received PID allowance 2048 while the new image was prepared; those old workers subsequently stopped. This temporary relief is recorded separately and is not the repair.

Resources now record current/max PIDs, PID-limit events, and sticky native-thread log evidence. Confirmed native-thread failure takes precedence over generic TPS diagnosis, including during startup and finalization. Independent artifact admission scans complete logs, so a recovered error cannot silently pass. Versioned admission policies re-audit existing delivered decisions in bounded batches after a controller upgrade.

The patched worker is `sha256:2663d0680b0b749a388a4b2d9509df797f88079845bc72f2811f5c8d8bb4ac3a`; the controller is `sha256:352ae90ba2f3215841c7de5365d6dcad9cf73b721c1c22d7134db83499d18e6b`. Both are available locally as their `:dev` tags. The operator changed the gallery target to zero during the audit; rollout preserved zero and retained all uncapped count/CPU/memory settings. The console still binds `0.0.0.0:8080`.

## Broader task mix

The catalog now contains 54 tasks, with 53 supported tasks enabled in the operating configuration. The mob-survey task remains prerequisite-gated and the ordinary-worker protocol fixture remains unsupported. Without surveyed mob tags, 52 tasks are eligible across the supplied bank, subject to existing cooldowns.

Twelve families cover gathering, crafting, tools, equipment, food, mining, exploration, progression, Creative, Adventure, Spectator, and operator actions. Added goals include planks/sticks and containers, stone/iron tools, shield/armor, food and cooking resources, smelting chains, travel destinations, and desert-temple search. Nether and full-game progression retain their long budgets. Item acquisition is not falsely counted as item use, fishing, or container interaction.

The scheduler accounts for explicit active reservations, balances task families so large item lists cannot crowd out mode families, distributes tasks within a family, and spreads assignments over less-used worlds. Coverage deficits, cooldowns, compatibility restrictions, and deterministic persisted assignments remain intact. A real-catalog dry preview selected all 12 families on 12 distinct bank worlds without launching workers or adding observations. New command capability is not a claim that every goal has completed in live play. Director sessions remain diagnostic when authoritative success is unknown.

## Verification

118 controller tests passed, including native-log admission revocation, immutable evidence preservation, automatic policy re-audit, pending-aware family rotation, and world distribution. Browser login, coverage search, responsive layout, and independent admission visibility were checked after rollout.

Two isolated real workers used no Docker CPU or RAM quotas, the default PID limit 512, normal rendering, and supplied bank worlds:

| Goal | World | Client mean TPS | Server mean TPS | Peak PIDs | PID-limit events | Admission |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Crafting table | world40 | 20.008 | 19.977 | 117 | 0 | Eligible |
| Stone pickaxe | world28 | 19.999 | 20.004 | 116 | 0 | Eligible |

Both completed their actual goals, flushed captures, retained periodic screenshots, and exited client/proxy/server/display zero. No native-thread errors appeared. The stone-pickaxe case exercised a newly enabled tool goal through actual crafting and resource acquisition. These two-worker checks do not substitute for a fresh 15-worker capacity test.

Evidence: `.state/task-diversity/` contains the admission re-audit, PID mitigation, JVM probe reference, assignment preview, unit/browser logs, live-fix summary, and live-test logs. Full isolated worker artifacts remain in `.state/thread-validation/artifacts/`. No saves, captures, tokens, or local databases were committed.

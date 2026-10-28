# Fleet validation — October 28, 2026

These are real Minecraft workers, not mocked backend tests. Failed exploratory runs remain alongside successful runs under ignored `.state/` directories. No captures, saves, access tokens, or runtime state were committed.

## Tested software and profile

- Worker: `sha256:3932fc2e802314d74d84169a0c07942ef1efb01900c0cd63976e4c613d75197a`, available locally as both `vibrato-worker:dev` and `vibrato-worker:modes`.
- Controller: `sha256:a2c409d295b95ca309890bb653ada86054181a8497323ca9bf5c59a5e4f6b3a3`, available as `vibrato-controller:dev`.
- Worker inputs include Vibrato `b53f401` and Conductor `b8c4572`; the later controller includes scheduler admission fixes through `e4b17a4`. Exact staged input hashes are retained in worker build metadata. The controller-only scheduler changes do not change the tested game runtime.
- Each worker: 8 CPUs, 6 GiB memory, 8 GiB sampled storage budget, software rendering with explicitly recorded `LP_NUM_THREADS=2`. Rendering, client, server, proxy, and supervisor all count against that worker's resources.
- Controlled task: actual Survival `@get dirt 1`, non-operator, followed by a bounded explicit post-task measurement wait. These measurements do not establish arbitrary autonomous workload capacity.

## Docker recovery

`scripts/test-docker-fleet-live.py` passed the complete sequence on the fixed worker:

1. Two workers reached measured healthy gameplay.
2. Restarting the controller adopted the same run IDs without duplicate containers.
3. Killing an owned container produced a fresh replacement that reached healthy gameplay.
4. The killed run's partial evidence was independently verified centrally and excluded from training.
5. Manual replacement preserved the desired count and produced a fresh healthy run.
6. Setting target zero drained every owned worker; all test evidence reached central storage.

Evidence: `.state/gallery/live-test-final.log` and `.state/gallery/live-results.json`. The earlier sequence is preserved in `recovery-before-settle-fix.json`, including incidental false-idle failures; it is not substituted for the corrected run.

A successful run, `20261028000553-83973e9582`, completed a full 180-second post-task wait and passed strict central-manifest verification and independent collection admission. Client: 90 measured intervals, 20.00665 mean TPS. Server: 91 intervals, 20.00160 mean TPS. Client, proxy, server, and display all exited zero. `.state/gallery/settle-proof.json` also records a healthy worker more than 110 seconds into that wait.

## Kubernetes recovery

The final kind test used the same worker image and final controller image, persistent claims, namespace-scoped controller RBAC, and one worker at a time. Its separate small test bank remained read-only; supplied user worlds were exercised by Docker and the mode scenarios.

The complete sequence verified authenticated readiness, actual healthy gameplay, controller restart adoption without a duplicate Pod, forced Pod deletion, conservative missing-capacity reservation, and a fresh replacement. The controller waited for stale heartbeat/grace and artifact quiescence before reclaiming absent-worker capacity. Interrupted evidence was centrally verified and excluded from training; missing-process inference was retained as inference.

The replacement, `20261028001656-69e7b80557`, acquired dirt and completed its planned 90-second settle normally. Independent central admission accepted it: client 19.99352 TPS across 39 intervals; server 20.00655 TPS across 41 intervals; 114,076 packets and 23,340,336 raw bytes, with complete C2S/S2C capture evidence. The initial interrupted run was `20261028001315-aec32bc5ca`. Continuous desired-count reconciliation briefly launched another run after normal completion; target zero drained it, and its stopped evidence was centrally verified and excluded.

A prior task cooldown also resumed automatically without erasing genuine failure history. During that wait, desired count stayed one, `pausedReason=no_eligible_task`, `nextEligibleWall` identified the deadline, readiness remained true, and the controller reported no reconcile error. Evidence: `.state/kubernetes/cooldown-readiness-proof.json`.

Final evidence is `.state/kubernetes/live-test-final.log` and `live-results.json`. The test ended at desired zero with no active worker Pods. The idle controller and isolated test cluster remain available for inspection.

## Four-worker capacity and gallery

`scripts/test-docker-capacity-live.py --exercise --workers 4 --hold-seconds 60` passed with the same four run IDs healthy throughout 30 samples over a one-minute window. Aggregate allocation was 32 CPUs and 24 GiB; starts were staggered eight seconds. Private clones used the supplied `world1` and `world2` bank entries. The fleet drained afterward.

| Run suffix | Rolling client TPS range | Rolling server TPS range | Peak sampled memory MiB |
| --- | --- | --- | --- |
| `f8dc045d58` | 19.967–20.031 | 19.993–20.009 | 2584.2 |
| `19d8161e25` | 19.924–20.119 | 19.994–20.006 | 2839.4 |
| `f4f582bd70` | 19.972–20.025 | 19.987–20.012 | 2746.5 |
| `e364ef1785` | 19.606–20.037 | 19.994–20.008 | 2704.3 |

Evidence: `.state/gallery/capacity-results.json` and `capacity-final.log`. Measurements include rendering and recording. This is a gathering-and-post-task baseline, not a claim that four simultaneous exploration, combat, or progression campaigns always meet the same threshold. Shipped general-purpose examples retain a one-worker ceiling until that deployment's workloads are measured.

The Playwright browser check passed while all four were healthy: authenticated login, scrolling to load every real thumbnail, an enlarged frame refreshed after 15 seconds, table/gallery switching, coverage-gap search, and mobile layout without horizontal overflow. No browser script errors occurred. Evidence: `.state/gallery/final-browser.log` and `final-browser/{desktop,mobile,inspection}.png`.

## Worlds, modes, and quality

All 50 supplied world fingerprints were rechecked after live testing and matched their import sidecars: 1,306,644,855 logical bytes. The source bank is `/opt/world-bank`; workers mount it read-only and clone into private writable directories. `.state/world-import/final-verification.json` contains the full check. No further worlds are needed for initial validation. Stored chunk bounds do not certify a complete pregeneration radius or biome/structure survey.

The [mode report](mode-validation-2026-10-27.md) records all five controlled mode/permission scenarios and a Creative-template-to-Survival override, independently admitted with real server evidence. It also records the crafting repair and the limits of the unsuccessful bounded Nether/End progression probes. Presence observations do not imply acquisition or interaction coverage.

Final controller unit suite: 102 tests passed. Java build/tests, recorder raw-wire tests, and protocol reader roundtrip validation are recorded with their respective changes. The reader's corrected recipe mapping roundtripped all 32,467 play packets in the tested capture, including the full recipe packet, without errors.

## Findings and limits

The live checks exposed and fixed cumulative tick sampling aliasing, loading/warmup contamination, excessive renderer threads under a CPU quota, a real crafting-input loop, navigation restart false positives, a hidden-thumbnail loading deadlock, and false idle detection during a completed task's planned wait. None of the failed probes were silently relabeled as successful data.

A known scheduler cooldown now pauses new admission without making controller readiness fail. Genuine task failure history and cooldowns remain intact; fleet Retry after fix does not erase them. Kubernetes bank examples mount compatibility metadata into both controller and workers so scheduling can enforce template restrictions.

The local filesystem is the selected dataset backend. Multi-node shared storage, ingress/TLS, external object storage, and NetworkPolicy enforcement on a production CNI remain deployment-specific work; the local kind cluster does not prove them. Long autonomous progression and exhaustive content-interaction coverage remain future campaigns.

## Local handoff

The authenticated console is running at `http://127.0.0.1:8080/`, target zero. Its token file is `.state/gallery/controller/dashboard.token` (keep its contents private). The operating configuration is `.state/gallery/operating-config.json`: all 50 bank IDs, the validated gathering/crafting and five mode/permission scenarios, 8 CPUs/6 GiB per worker, two renderer threads, and a conservative one-worker starting ceiling. Historical test runs remain inspectable. The four-worker validation configuration remains `.state/gallery/config.json`; do not run a second controller against the same state.

All 23 Docker run manifests were independently verified in central storage after the final drain. The operating console was browser-checked again after restart, and reported no reconciler or delivery errors. No Minecraft worker is left running in the Docker fleet. Git commits use `Codex <codex@openai.com>` and nothing was pushed.

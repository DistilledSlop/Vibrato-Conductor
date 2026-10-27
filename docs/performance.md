# Optional optimization evaluation

The default worker remains the baseline with no performance mods. Candidate profiles are explicit opt-in experiments and are excluded from training until behavior equivalence is reviewed against recorded scenario evidence. A faster tick rate or lower memory use alone does not establish unchanged gameplay.

## Pinned candidates

The [FerriteCore 7.0.3 Fabric release](https://modrinth.com/mod/ferrite-core/version/sOzRw3CG) and [Lithium 0.15.4 for Minecraft 1.21.1](https://modrinth.com/mod/lithium/version/N08Z8wog) explicitly list Minecraft 1.21.1/Fabric and both client/server support. Their downloaded `fabric.mod.json` files declare loader minimums satisfied by this project's 0.16.14 loader. The exact releases, official SHA-512 values, independently calculated SHA-256 values, artifact sizes, dependencies, licenses, and source URLs are pinned in `configs/optimization-profiles.json`.

FerriteCore targets memory representation and caching; its [technical summary](https://github.com/malte0811/FerriteCore/blob/main/summary.md) describes changes to block-state and model data. Those historical example savings are not measurements of this fleet. Lithium changes simulation implementation with a stated vanilla-behavior goal, and exposes configurable patches; see its [official project](https://github.com/CaffeineMC/lithium) and [configuration documentation](https://github.com/CaffeineMC/lithium/wiki/Configuration-File). That goal is not accepted here as proof of equivalence with Vibrato, the proxy, this mod set, or these scenarios.

## Explicit staging and derivative images

```bash
python3 scripts/optimization-eval.py list
python3 scripts/optimization-eval.py stage --profile ferritecore \
  --output .state/ferritecore-stage
python3 scripts/optimization-eval.py build --profile ferritecore \
  --base-image vibrato-worker:dev --tag vibrato-worker:experimental-ferritecore
```

Profiles are `baseline`, `ferritecore`, `lithium`, and `ferritecore-lithium`. The tool verifies both hashes and the mod descriptor before use. It refuses to overwrite the baseline image tag or stack candidates on an already optimized base. Candidate images add only the pinned mods to both installed mod directories and record provenance in `/opt/optimization-profile.json`, `versions.optimizationMods`, `versions.optimizationProfile`, and `build.optimizationBuild`.

The experimental image entrypoint forces `MODE=diagnostic` while its profile remains unvalidated, even if an invocation requests collection. No default game rules, rendering profile, task semantics, or worker input files are changed by this tool. Experimental runs should retain baseline resource limits and use fresh clones of the same immutable bank revision.

## Measurements and evidence

```bash
# During an owned Docker worker run, store this outside its immutable artifact directory:
python3 scripts/optimization-eval.py sample --container vibrato-RUN_ID \
  --output .state/optimization-evaluation/RUN_ID.monitor.jsonl --duration 180
python3 scripts/optimization-eval.py summarize .state/artifacts/RUN_ID \
  --monitor .state/optimization-evaluation/RUN_ID.monitor.jsonl \
  --output .state/optimization-evaluation/RUN_ID.summary.json
python3 scripts/optimization-eval.py compare \
  --baseline /mount/baseline/RUN_ID --candidate /mount/candidate/RUN_ID \
  --baseline-monitor .state/optimization-evaluation/BASELINE.monitor.jsonl \
  --candidate-monitor .state/optimization-evaluation/CANDIDATE.monitor.jsonl \
  --output .state/optimization-evaluation/comparison.json
```

Summaries verify immutable manifests and retain client/server cadence, authoritative task/scenario outcomes, actual packet/content observations, quality reasons, CPU consumption for the complete worker cgroup, and cgroup memory. Optional Docker process monitoring sums RSS across the supervisor, client, server, proxy, display, and init processes. Shared pages may be counted by more than one process. Without those samples, RSS is explicitly unavailable; cgroup memory is not mislabeled as RSS. Kubernetes per-process RSS still needs its own observation path.

Compare the same assignment, seed, immutable world revision, mode, permissions, render profile, source/jar hashes, resource limits, and budgets. Monitor files must identify the corresponding run and consistent limits; when comparing several runs, supply one monitor per run in the same order. Missing or unbounded CPU/memory limits keep a pair unready for scenario review. Record startup and steady intervals separately. Repeat matched scenarios; one successful example does not establish rare behavior equivalence. Observation differences are review signals and may reflect nondeterminism.

The report always leaves `behaviorEquivalent=false` and `behaviorValidation=unvalidated`. It lists missing required behavior scenarios, including physics, mob AI, redstone, containers, dimension changes, long progression tasks, and the explicit mode/permission curriculum. Those need scenario-specific authoritative evidence and review before a candidate can be promoted. Profiles are not automatically approved from a benchmark percentage.

Current checkpoint: official artifacts and hashes were verified, staging and offline baseline analysis work, and focused tool tests pass. A FerriteCore derivative image was built and checked under its unprivileged runtime user: both mod copies match the pinned hashes, metadata is readable, and requesting collection still enters diagnostic mode.

The bounded `ferritecore-dirt-01` smoke run loaded FerriteCore 7.0.3 on both client and server,
completed the real Survival `@get dirt 1` task with dirt in inventory, and shut down cleanly.
Measured limits were 8 CPU cores and 6 GiB; elapsed time was 53.45 seconds, whole-worker CPU
consumption 256.50 core-seconds, reported cgroup memory peak 2,490,675,200 bytes, and sampled
aggregate process RSS peak 2,532,921,344 bytes. RSS monitoring began after startup and sums shared
pages across processes; it is not unique physical memory. This is a compatibility smoke test.
Diagnostic scripts currently exit after task completion without the collection settling interval,
so this fast task supplied zero post-warmup quality samples. Tick capacity, performance differences,
and behavior equivalence remain unvalidated; the quality gate excludes the run from training.
A valid later benchmark needs an explicit settling interval in diagnostic mode and matched
baseline software, limits, task budget, and starting world. No optimization is enabled by default.

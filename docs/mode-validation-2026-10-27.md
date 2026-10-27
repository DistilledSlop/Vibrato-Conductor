# Live mode and permission validation — October 27, 2026

Six real worker runs passed independent collection admission, strict artifact manifests, captured packet/index integrity, and periodic framebuffer PNG checks. Every run used 8 CPUs, 6 GiB, normal rendering, and an explicitly recorded `LP_NUM_THREADS=2`; FPS, view distance, game rules, and simulation mechanics were not reduced. Tick rates use cumulative counter deltas weighted by elapsed time after the 20-second game-ready warmup.

| Run | Mode | OP level | Client TPS | Server TPS | Packets | Outcome |
| --- | --- | --- | --- | --- | --- | --- |
| `mode-creative-inventory-01` | creative | 0 | 19.975 | 20.003 | 62,844 | Passed: Creative inventory and flight |
| `creative-template-survival-01` | survival | 0 | 19.984 | 20.007 | 77,019 | Passed: Creative template overridden to Survival |
| `mode-creative-build-01` | creative | 0 | 19.988 | 20.007 | 32,502 | Passed: Creative block placement |
| `mode-adventure-01` | adventure | 0 | 19.992 | 20.007 | 36,087 | Passed: Adventure block-breaking restriction |
| `mode-spectator-01` | spectator | 0 | 19.994 | 20.006 | 26,271 | Passed: Spectator flight through terrain |
| `mode-operator-01` | survival | 2 | 19.980 | 20.007 | 34,038 | Passed: Explicit level-2 operator command |

Total: 268,761 captured packets and 107,993,425 raw bytes. Every run retained four observed screenshot updates and ended with client, proxy, server, and display exit code zero.

## Actual goal evidence

- Creative inventory: the server confirmed a 64-stone stack and flying enabled; actual ascent was Y=113 to Y=118.242. The player had permission level 0. This proves inventory interaction and flight, not survival acquisition.
- Creative build: on the supplied `/opt/world-bank/world1/world`, the server confirmed air changed to stone at `[471,64,222]` after a real client hand/block interaction. The player remained a non-operator.
- Adventure: on the same supplied world, a block-dig attempt targeted dirt; the server confirmed the block stayed unchanged after two seconds and `allowModifyWorld=false`. The denied attempt is not mislabeled successful block modification.
- Spectator: server-confirmed flying and `noClip` accompanied descent from Y=63 to Y=54.562 through unchanged stone at ground Y=62. The player had permission level 0.
- Operator: exactly the assigned offline identity received level 2 in `ops.json`. In Survival, `/time add 1200` changed server-observed time from 1480 to 2700. Other workers were not granted operator privileges.

## Creative pregeneration remains compatible with Survival

The Creative inventory worker saved its private world cleanly. Its `level.dat` was parsed independently and reported `GameType=1` (Creative), Minecraft data version 3955. That saved world became the read-only `creative-pregen-verified` bank entry. A fresh Survival worker cloned it, entered actual Survival with permission level 0 and normal Survival abilities, acquired one dirt, and passed collection admission. Its own saved `level.dat` reported `GameType=0` while the source remained Creative.

The supplied user world `world1` was also validated by the Creative build, Adventure, Spectator, and operator cases. All four runs recorded the same immutable input world revision. Private worlds were isolated for each run.

## Quality and earlier diagnostic findings

- Earlier 4-CPU probes exposed genuine rendering/tick pressure, and one initial implementation mixed terrain-loading samples into capacity windows. Game readiness and warmup are now explicit. Cumulative counter rates replace aliased one-second snapshots sampled every two seconds.
- The original software renderer spawned 32 llvmpipe threads under the CPU quota. The recorded 8-CPU/two-render-thread profile passed these cases; this establishes the tested workload profile, not arbitrary fleet capacity.
- A bounded `@nether` probe correctly ended as `task_budget_exceeded`, retained evidence, and stayed outside the dataset. It did not prove Nether entry.
- A healthy-timing `@gamer` probe exposed a real crafting loop: normal inventory supply incorrectly included occupied 2x2 crafting inputs, causing planks to oscillate between grid slots. The corrected slot filter and immediate missing-recipe-book fallback were committed, and a subsequent real crafting-table task passed. Neither the full End progression nor game completion was claimed.
- Fast restart inference is limited to matching inventory/crafting leaf objectives. Moving-target navigation restarts alone no longer cause a fast failure. All inferred classifications remain distinguishable from confirmed task errors.
- Failed worlds can now be archived after server shutdown with strict size/file/time bounds before manifest publication. The archive helper preserved an actual stopped crafting-failure world: 25 files, 15,751,220 logical bytes, 9,611,069 archive bytes, in about 0.28 seconds. Hard-killed workers cannot execute this cleanup path.

Local evidence remains under `.state/telemetry-live/artifacts/RUN_ID/`; it is ignored by Git. Each directory includes exact software/input hashes, requested mode and privilege context, server/client heartbeats, `scenario-result.json` where applicable, raw captures, screenshots, result, and manifest. See [worker telemetry](worker-telemetry.md) for reproducible commands and field semantics.

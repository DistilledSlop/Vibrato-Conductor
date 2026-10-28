# Resume validation, October 28, 2026

Run on the `vibrato` host with isolated single workers (`scripts/test-worker-live.py`, fresh run IDs, no CPU/RAM quotas, `LP_NUM_THREADS=2`). Failed runs and their artifacts are retained under `.state/mechanics-live/` and `.state/telemetry-live/`. The production controller was not restarted and no production worker was started; see "State left for the operator".

## Source fixes (Vibrato `master`, all pushed)

| Commit | Change | Why |
| --- | --- | --- |
| `851d389` | `MovementProgressChecker` bounds the "Baritone is calculating a path" excuse to 15 s without moving 2 blocks | While a search kept failing and restarting (about 288k nodes every 3 s) the checker reset forever, so a 300 s stall never reached the unreachable-block handling. |
| `e23e8cc` | `StoreInAnyContainerTask` ignores containers more than 12 blocks vertically or 48 horizontally away and places its own chest instead | Focused deposits chased dungeon chests tens of blocks underground. |
| `34b88f5`, `df53ceb` | Ore smelts get a 20 minute base budget, other smelts 10 | 4 minutes expired while mining tools and ore. |
| `593e83a`, `cf35779` | Iron-gear activities (enchant, anvil rename, grindstone) get 25 minutes | They must mine iron and craft the tool first. |
| `a88ff5c` | Builders get a 10 minute base budget | Budgets assumed materials were already in hand. |
| `38cfb98`, `2e13d89`, `df53ceb` | `FindBuildSiteTask` finds ground by scanning block states, retries and relocates up to 4 times | The client's `MOTION_BLOCKING_NO_LEAVES` heightmap reads as the world floor for every column, so builders were always sent to y=-64 and stalled. |

Jar `908856da…` (and later builds) were built from these commits; worker image `vibrato-worker:final-candidate-7` (`c6e29fb1b383`) contains all of them plus the final embedded Baritone (`8e6bd47d…`).

## Results

Python suite 130 tests; Vibrato and Baritone Java suites pass. Live isolated outcomes:

| Run | Outcome |
| --- | --- |
| `focused-smelt-iron-c3-world28` | **Pass**: task success and independent admission eligible. |
| `mech7-smelt_glass-world28` | Task success, place/smelt mechanic proven. Dataset-ineligible: sticky degraded TPS windows in the first ~30 s, while up to 11 workers were starting at once. |
| `mech7-build_wall-world44` | Task success, `place_block` mechanic proven. Ineligible for the same startup-contention reason. |
| `smelt_iron_ingot` on world40 | Failed: `PlaceBlockTask: No usable block to place … furnace` at y=38 in a cave. World/context specific. |
| `smelt_charcoal` | Failed: 6 minute crafting loop (logs, planks, sticks fuel accounting). Not fixed. |
| `chest_store_items` (worlds 15, 35, 40) | Failed. The bot now places and opens its own chest, but the server repeatedly opens and immediately closes it (about 600 open/close pairs) and the deposit never registers. Not fixed. world40's spawn is also a pond with dirt only on the floor. |
| `build_hut` | Failed: budget exhausted trying to path to cobblestone about 16 blocks below. |
| `anvil_rename`, `enchant_item`, `breed_animals`, `slay_mob` | Failed: death at night while foraging, or budget. |
| `fish`, `trade_for_item`, `trade_for_enchanted_book`, `farm_crop` | Failed (rod prerequisite and watchdog trip, villager would not open trades, village search budget, semantic stall). |
| `@nether` on world4 (`nether-final-world4-05`, `-06`) | **No Nether entry.** Run 05 died four times to night mobs; run 06 survived 3321 s, then oscillated digging down and pillaring up around y=-7 to -11 and was ended by the `nerd_pole_no_height_gain` watchdog. The resumed checkpoint `nether-waterfall-resume-04` stalled on a deep "Destroy block" target before the path-calculation fix. |

The Nether remains unproven. Deaths to skeletons and zombies in the first minutes recur across many failed runs (world times are fixed by the saved worlds), and any death aborts a focused activity.

## Capacity note

The production audit of 69 runs shows 11 of 12 overload failures occurring in one five minute window with 16 to 21 workers running, before the thread-pool fix. Start the fleet at a modest worker count and raise it while watching the client/server overload rate; do not jump straight to a large target. The strict admission gate correctly excluded runs whose first seconds were degraded.

## State left for the operator

- Controller PID in `.state/gallery/controller.pid` is the **old process, never restarted**, so the corrected pending-buffer accounting is not live. Its persisted desired count is 15; the fleet is paused at 0 actual workers.
- `.state/gallery/operating-config.json` was edited once (backup `operating-config.json.bak-20261028-040231`, catalogs backup `catalogs.bak-20261028-040231`): the runtime catalog was refreshed from the repo and 14 `mechanic-*` ids were appended to `enabled_tasks`. Most of those failed above. A second edit to apply the validated mix was blocked. To apply it before restarting:

```bash
cd /opt/Vibrato-Conductor/.state/gallery
python3 - <<'PY'
import json
p = "operating-config.json"
c = json.load(open(p))
keep = {"mechanic-smelt-glass", "mechanic-smelt-iron-ingot", "mechanic-build-wall"}
c["enabled_tasks"] = [t for t in c["enabled_tasks"] if not t.startswith("mechanic-")] + sorted(keep)
c["image"] = "vibrato-worker:final-candidate-7"
json.dump(c, open(p, "w"), indent=2)
PY
```

- Then restart the controller (`python3 -m conductor serve --config /opt/Vibrato-Conductor/.state/gallery/operating-config.json --host 0.0.0.0 --port 8080`, never a second instance) and confirm the false `artifact_storage_backpressure` pause clears.
- Images added: `vibrato-worker:final-candidate` through `final-candidate-7`. Existing `:dev`, `:focused` and `:nether-*` tags were not retagged.

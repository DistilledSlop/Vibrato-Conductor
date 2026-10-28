# Production gameplay failure fixes,2026-10-28

Production Nether run `20261028010221-8660555ee4` on user `world4` failed after135 seconds with `PlaceBlockTask: No usable block to place ... furnace`. Its configured task budget was3600 seconds and both games ran near20TPS. Production Gamer run `20261028010036-0f7003d694` on `world39` hit the same furnace handoff failure, also before its task budget. These were gameplay failures, not successful goals or timeout-only probes.

The shared cause was an ancestor seeing the crafted furnace on the cursor, switching from crafting to furnace placement, and closing the crafting screen during the old task's cleanup. The existing placement grace could not see a screen that an ancestor had already closed. The Nether run's next heartbeat showed the furnace in main inventory two seconds after the root had failed.

Vibrato commit `7576b27` makes the crafting output receiver retain ownership while its specific output remains on the cursor. It moves the stack into player inventory before ancestors can close the crafting handler or start placement. Regression tests run the real receiver's interruption contract through both placement replacement and null cleanup. Goal predicates, failure propagation and time budgets remain intact.

Creative-build runs including production `20261028010211-a272e20e4d` (`world34`) and `20261028003932-1a81b3d6e3` (`world48`) rejected canopy spawns because the selector required a full-solid block state. Leaves have usable collision geometry. Commit `729526f` selects nonempty collision supports, validates the actual raycast top-face hit within the player's reach, and avoids placing inside the player's box. It still requires independent server-confirmed air-to-stone change within40 seconds. Geometry regressions reject obstructed, wrong-face, distant and player-intersecting candidates.

Validation used frozen image `vibrato-worker:nether-fix`, ID `5b68217d790b9657dba5e8c00afab0e74804c69e4dd9175c4860fd0f0d32b0f6`, with clean source provenance. Both isolated workers used normal rendering, explicit `LP_NUM_THREADS=2`, JVM pool sizing8, and no CPU/RAM quotas. The active gallery target and its workers were not changed by these tests.

| Isolated run | Actual evidence | Client/server TPS | Result |
| --- | --- | --- | --- |
| `furnace-handoff-world4-01` | Furnace output PICKUP, explicit cursor-to-inventory storage, then furnace placement and actual iron_ingot1 |20.005/19.999;51/53 measured intervals | Task success; worker and independent dataset admission passed |
| `creative-build-world34-01` | Raycast-confirmed oak_leaves UP support; server air-to-stone at[0,74,-9], Creative nonoperator0 |19.969/20.008;13/15 measured intervals | Task success; worker and independent dataset admission passed |
| `creative-build-world48-01` | Raycast-confirmed grass_block UP support; server air-to-stone at[40,67,-61], Creative nonoperator0 |19.996/20.006;14/15 measured intervals | Task success; worker and independent dataset admission passed |

All three runs have complete captures, strict verified manifests, real periodically updated PNG screenshots and all four processes exiting0. The furnace run peaked at114 PIDs against the512 limit with no limit events or native-thread failures.

A full `@nether` run, `nether-handoff-world4-01`, was subsequently started on the same user world with an honest1800-second goal budget. It passed the original failure stage: crafted/stored/placed a furnace, smelted all seven initial iron, crafted buckets and flint-and-steel, then obtained water and an additional empty bucket. At the time of this report it is still in progress toward lava collection; the furnace regression's success does not assert completion of the Nether journey. Evidence is retained under `.state/telemetry-live/artifacts/<run>/`.

# Admitted packet coverage gaps — October 28, 2026

A historical read-only audit of the gallery coverage database and current manifest-bound policy-v4 admission receipts found **33 of 57 model-output packet types** and **17 of 30 input/input-ack packet types** in admitted runs. The same database contained raw observations of 36 output and 18 input types across all ingested runs, including excluded diagnostics. Counts are point-in-time and can change as admission or collection changes; raw observations are not training admission. The later snapshot in the CURRENT section of [the reliability ledger](RELIABILITY-WORK-2026-10-28.md) recorded admitted coverage of 36/57 output and 17/30 input types; consult that ledger and re-audit for current counts. The missing-type lists below describe the earlier audit, not the later snapshot.

These numbers describe actual Play wire-type presence, not the decoded training rows or all model-owned branches of HYBRID packets. They do not include login/setup or packet classes that the reader classifies as ENGINE, BAKED, DERIVED or DROP. A task's declared packet target does not count as evidence that it was emitted.

## Missing admitted model-output types

| Clientbound ID | Reader packet name |
| ---: | --- |
| 6 | `block_break_animation` |
| 10 | `boss_bar` |
| 15 | `clear_titles` |
| 23 | `set_cooldown` |
| 30 | `profileless_chat` |
| 32 | `explosion` |
| 35 | `open_horse_window` |
| 41 | `world_particles` |
| 45 | `trade_list` |
| 50 | `open_book` |
| 68 | `reset_score` |
| 76 | `action_bar` |
| 87 | `scoreboard_display_objective` |
| 89 | `attach_entity` |
| 94 | `scoreboard_objective` |
| 96 | `teams` |
| 97 | `scoreboard_score` |
| 99 | `set_title_subtitle` |
| 101 | `set_title_text` |
| 102 | `set_title_time` |
| 103 | `entity_sound_effect` |
| 106 | `stop_sound` |
| 109 | `playerlist_header` |
| 118 | `entity_effect` |

## Missing admitted input and input-ack types

| Serverbound ID | Reader packet name |
| ---: | --- |
| 5 | `chat_command_signed` |
| 6 | `chat_message` |
| 13 | `enchant_item` |
| 16 | `set_slot_state` |
| 20 | `edit_book` |
| 30 | `vehicle_move` |
| 31 | `steer_boat` |
| 32 | `pick_item` |
| 38 | `steer_vehicle` |
| 42 | `name_item` |
| 45 | `select_trade` |
| 46 | `set_beacon_effect` |
| 53 | `update_sign` |

## Follow-through

The catalog already associates the focused trade goals with `trade_list` and `select_trade`, enchanting with `enchant_item`, anvil rename with `name_item`, and boat travel with vehicle movement/steering inputs. These are candidates for bounded real canaries, not guarantees of packet emission. Trade, enchant and anvil goals need successful, independently admitted outcomes; boat travel additionally requires a verified crossing context. The catalog also lists `attach_entity` for boat travel, but that packet represents an entity link/leash; actual boat passenger behavior uses `set_passengers`. A boat assignment alone does not demonstrate `attach_entity`.

The original 50 world entries have no prepared-context tags. Inventory sorting, loot-dependent deposits, withdrawals, grindstone use and boat travel therefore cannot be scheduled on those entries. A separately derived earned inventory-sort template was tested with two distinct identities and has since been registered, restricted to the organize task with its source-admission checks intact. Prepare other contexts from verified natural gameplay and bind their provenance before enabling them; do not add tags without the underlying state.

The remaining missing types have no focused packet target or demonstrated task adapter in the current catalog. They include horse inventory; book, sign and beacon interaction; chat; explosion, particles, effects and sounds; and bossbar, scoreboard, title and related display behavior. They need individual finite, observable goals and appropriate real world or authorized operator contexts. Signed chat specifically requires a compatible authenticated signed-chat session; the current offline collection environment does not establish that context. Do not fabricate packets or relax admission to fill these gaps.

Continue by validating the existing trade/enchant/anvil/boat canaries, then add narrowly scoped real mechanics for the remaining gaps. Separately measure decoded reader-row and HYBRID-branch coverage, which cannot be inferred from packet IDs alone. The existing representative dataset's rotations and TimeSynth are preprocessing augmentation, not additional raw capture or mechanic proof.

# Coverage catalogues

The inventory explicitly distinguishes requested targets, actual observations, and authoritative task outcomes. Scheduling an assignment does not fill a coverage target.

`protocol-767.json` is copied from Vibrato's protocol 767 manifest. It retains its source and CC BY-SA attribution fields. Protocol targets use phase, direction, and numeric wire ID; proxy packet names are not assumed to match human manifest names. The recorder does not fully preserve handshake, status, and login; unsupported phases remain visible as requested gaps.

`registries-1.21.1.json` was generated from the `minecraft-data` installed by Protocol Transformer's pinned proxy lockfile. It contains the Minecraft 1.21.1 item/block/entity names used by this inventory. `obtainable-items.txt` is the existing Vibrato resource catalogue, used conservatively for exact command capability matches. Generic aliases are not silently mapped to every concrete variant.

`tasks.json` contains existing command forms: seeded director runs, `@get RESOURCE COUNT`, `@nether`, `@gamer`, `@food COUNT`, `@goto X Z`, `@locate_structure DESERT_TEMPLE`, and the controlled `@conductor_scenario NAME` commands listed below. Mob-survey worlds require a `mobs` tag. The protocol fixture suite is explicitly unsupported for ordinary worker images. A matching immutable bank entry can declare tags and a required full revision in `WORLD_ID/world.json`.

Item presence means the item was in the sampled inventory; a block observation means the crosshair actually targeted it; a mob observation means a live loaded entity was nearby. These observations never prove acquisition, use, inventory interaction, block interaction/state change, or mob interaction. Those facets are separate requested targets. Most remain unsupported until authoritative action-specific instrumentation supplies their evidence; controlled Creative scenarios can confirm stone inventory interaction, stone item use, and stone state changes through the server observer. Positive inventory deltas alone do not prove acquisition cause. Task success belongs only to the task target and is never copied into unrelated content-interaction targets.

Collection admission is independent: raw capture completeness, immutable artifact integrity, explicit collection mode, authoritative task success, measured healthy game ticks, software/world identity, and worker approval must all pass. Diagnostic or inferred outcomes remain excluded even if they supply useful observed coverage.


## Modes and permissions

Each task declares its game mode and operator requirement. Existing resource/progression tasks remain Survival with no operator privileges. The controlled scenarios are:

| Scenario | Mode | Operator |
| --- | --- | --- |
| `creative_inventory` | Creative | No |
| `creative_build` | Creative | No |
| `adventure_inspect` | Adventure | No |
| `spectator_explore` | Spectator | No |
| `operator_commands` | Survival | Level 2 only |

Assignments carry `gamemode`, `operator`, `operatorLevel`, and `scenario`; a false operator flag never grants operator privileges. World metadata may restrict `gamemodes` or set `allowOperator=false`. A clean Survival world bank can serve as the immutable source for each isolated mode-specific clone. Creative scenarios are never scheduled against a world restricted to Survival.

Coverage records actual observed game modes and abilities, server-observed permission levels, and task/mode/permission contexts independently. Merely assigning Creative or configuring an operator flag earns no observation. Permission contexts require the expected exact server level (zero for ordinary workers; two for the operator-command scenario). Client-only permission assumptions are not authoritative coverage. Levels one, three, and four, and unconfirmed command-denial outcomes remain requested unsupported gaps.

A scenario outcome requires a completed `scenario-result.json` with matching scenario identity, client-action/server-observer provenance, server confirmation, and an actually observed matching mode/permission context. Only specifically confirmed action events credit interaction targets. Adventure denied breaking does not count as successful block interaction, and Spectator pass-through requires the controlled movement proof rather than a mode flag alone.

## Task variety

The catalog has 54 assignments across 12 supported task families: gathering, crafting, tools, equipment, food, mining, exploration, progression, Creative, Adventure, Spectator, and operator commands. Survival goals include planks/sticks and containers, stone/iron tools, shield/armor, food collection and cooking ingredients, furnace/smelting chains, travel destinations, structure search, Nether entry, and full progression. Resource names are checked against the existing obtainable-item catalog and Minecraft item registry; supported command capability does not claim every goal has completed in a live run. Boat, fishing-rod, or container acquisition is not proof of using those items.

Each task can declare `family`; omission preserves the task ID as its own family. Scheduling balances currently pending/live families and tasks, then lifetime family allocation and coverage deficits. A family containing many item variants cannot crowd out a smaller mode family. Bank selection spreads active assignments and prefers less-used world/task combinations with reproducible tie-breaking. Compatibility restrictions, task cooldowns, persisted retry assignments, and evidence-only coverage remain enforced.

Long Nether/End and structure goals retain their longer budgets. Seeded director sessions remain exploratory diagnostic evidence unless an independently supported success/admission rule is satisfied. The mob-survey task still needs surveyed `mobs` tags; the protocol fixture suite remains unsupported in ordinary workers.

## Focused mechanic episodes

`@activity ID` requests one specific mechanic goal through the existing real activity implementation. The finite plan contains only that goal and its prerequisite providers. The focused_small_v1 parameter profile keeps quantities small and relevant (for example, a few cheap items to deposit, a few smelts, one trade, or a short boat trip); ordinary Director sampling remains unchanged. The catalog includes23 such assignments spanning14 mechanic targets. The recorded activity plan preserves actual sampled parameters and budgets.

Each focused assignment declares `mechanics` and canonical `packet_targets`. Targets affect scheduling but never count as evidence. Mechanic proof combines a completed specific goal with scoped successful client actions and an outcome appropriate to the mechanic. Generic completion, movement, timer expiry, inventory presence, and declared packet expectations cannot stand in for that proof. Client proof is labelled client_action_and_task_outcome; it is not relabelled server-confirmed.

Inventory sorting/withdrawal and certain stash goals require prepared inventory or surveyed loot contexts. Grindstone needs pre-enchanted input; boat travel needs a water-crossing context. Those contexts are explicit `requiresTags` and are not asserted on the generic bank. The implementation refuses no-op completion instead of awarding success on an empty inventory or a walking fallback. Advanced workstation and trading goals still need actual supplies/stations/offers, obtained through their prerequisite paths where supported.

`model-packets.json` pins the Protocol Transformer reader classification:57 MODEL/HYBRID output types and30 INPUT/INPUT_ACK conditioning types. ENGINE, BAKED, DERIVED and DROP are excluded from this training-target subset; the full224 raw protocol inventory remains separately inspectable. The dashboard shows model output/input observations and per-task targeted types under Mechanic packets. These are observed wire types, not proof of every semantic branch or decoded model row of a HYBRID packet. Packet presence and successful mechanic goals are separate counts accumulated across runs.

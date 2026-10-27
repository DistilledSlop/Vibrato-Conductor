# Coverage catalogues

The inventory explicitly distinguishes requested targets, actual observations, and authoritative task outcomes. Scheduling an assignment does not fill a coverage target.

`protocol-767.json` is copied from Vibrato's protocol 767 manifest. It retains its source and CC BY-SA attribution fields. Protocol targets use phase, direction, and numeric wire ID; proxy packet names are not assumed to match human manifest names. The recorder does not fully preserve handshake, status, and login; unsupported phases remain visible as requested gaps.

`registries-1.21.1.json` was generated from the `minecraft-data` installed by Protocol Transformer's pinned proxy lockfile. It contains the Minecraft 1.21.1 item/block/entity names used by this inventory. `obtainable-items.txt` is the existing Vibrato resource catalogue, used conservatively for exact command capability matches. Generic aliases are not silently mapped to every concrete variant.

`tasks.json` contains only existing command forms: seeded director runs, `@get RESOURCE COUNT`, `@nether`, and `@gamer`. Mob-survey worlds require a `mobs` tag. The protocol fixture suite is explicitly unsupported for ordinary worker images. A matching immutable bank entry can declare tags and a required full revision in `WORLD_ID/world.json`.

Item presence means the item was in the sampled inventory; a block observation means the crosshair actually targeted it; a mob observation means a live loaded entity was nearby. These observations never prove acquisition, use, inventory interaction, block interaction/state change, or mob interaction. Those facets are separate requested targets, marked unsupported until authoritative action-specific instrumentation supplies their evidence. Positive inventory deltas alone do not prove acquisition cause. Task success belongs only to the task target and is never copied into unrelated content-interaction targets.

Collection admission is independent: raw capture completeness, immutable artifact integrity, explicit collection mode, authoritative task success, measured healthy game ticks, software/world identity, and worker approval must all pass. Diagnostic or inferred outcomes remain excluded even if they supply useful observed coverage.

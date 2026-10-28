# Admitted capture integration

An isolated worker's successful capture becomes training data only after verified enrollment. To verify, deliver and index one completed collection run through the configured gallery spool and delivery queue:

```bash
python3 -m conductor import-training-run --config .state/gallery/operating-config.json .state/mechanics-live/artifacts/RUN_ID
```

The operation verifies the complete source and a current independent quality assessment, copies it atomically to the configured spool, uses compact delivery and admission receipts, and ingests coverage. After verification and terminal/runtime checks, redundant isolated per-run artifact and workspace copies can be cleaned. Existing run IDs must have the same manifest. An existing stale or revoked central receipt is not overridden. Repeat the command after a transient delivery failure; it is idempotent. Isolated live probes may opt into this behavior with `--enroll-config .state/gallery/operating-config.json`; ineligible results are retained but are not enrolled. The existing controller and configured paths determine retention and delivery, so use the same spool and queue rather than pointing one shared queue at different source directories.

Generate an auditable selection without preprocessing:

```bash
python3 -m conductor export-training-index --central .state/gallery/dataset --output .state/admitted-training-index-current.json
```

The index records manifest, receipt, binary and packet-index hashes, source world and assignment, accepted input base paths, rejection reasons, and a deterministic world-seed split. Repeat `--run-id RUN_ID` to restrict export or preprocessing to an explicit subset; a selected ineligible run prevents the dataset build rather than silently disappearing. A current eligible independent receipt bound to the manifest and a fresh integrity/quality check are both required. Stale receipts, revoked runs and corrupted runs are rejected. All captures from the same source world seed, including resumed checkpoints with that seed, stay on the same side of the split. Fewer than two admitted world groups gives an empty validation set and `ready: false`; no split is invented. The index is a point-in-time snapshot: regenerate it after admission changes. The October 28 snapshot and its verification sidecar are `.state/admitted-training-index-current.json` and `.state/admitted-training-index-verification-current.json`; the snapshot accepted 289 runs and rejected 184, with 218 training and 71 validation sessions. A later admitted run arrived after the checkpoint and is not in that index. Seven runs with missing banked-world source seeds remain excluded pending provenance; seven native-thread failure runs remain excluded.

To opt in to preprocessing, use a local worker image with Node and proxy dependencies or install the Protocol Transformer reader dependencies as described in that project's README, then run:

```bash
python3 scripts/build-admitted-dataset.py --central .state/gallery/dataset --output /path/to/new-dataset --runtime-image vibrato-worker:reliability-candidate-02
```

The builder chooses its preprocessing worker count automatically; pass `--jobs N` to override it. Admission checks use up to eight threads by default, independently configurable with `--index-jobs N`. Both admission checks still validate the full input set, before and after preprocessing. The optional `--runtime-image` uses Node and the pinned proxy packages already in a local worker image, with networking disabled and the source and central data mounted read-only; omit it to use a local Node installation and reader dependencies. This calls the Protocol Transformer dataset builder on the verified capture base paths, augments only training captures, produces `val.txt` and embeds the source index. It checks admission again before publishing and refuses to overwrite an existing dataset. It does not train a model. The standard `preprocess.sh` only searches `proxy/data` and does not consume the central delivery store; do not copy or glob central runs into it, because the central store also retains excluded evidence. To train separately, use the transformer's `train/train.py --dataset /path/to/new-dataset --val ...` with the session names in `val.txt`, after confirming the included admission receipts remain current and eligible. Model training and deployment are separate actions.

For an earned-context template, the original run's current central admission remains authoritative. Scheduling and worker preparation fail closed when that receipt is missing, stale, inconsistent or revoked. Derived-run delivery and export check the source receipt again, and the controller re-audits a delivered descendant when source admission changes. The template's embedded original receipt is provenance, not a substitute for the live receipt. Kubernetes workers without a read-only admission mount cannot use an earned template.

For a read-only coverage audit of the current independent admission receipts, run
`python -m conductor coverage-report --db PATH/coverage.sqlite3 --central PATH/dataset`.
The report snapshots the database and labels admitted counts separately from raw
observations. Without `--central`, admitted counts fail closed to zero while raw
diagnostic counts remain available. Use `--catalog-dir` when the controller uses
an operating catalog different from the bundled one.

## October 28 operating evidence

The point-in-time admitted index is `.state/admitted-training-index-v4-final.json`: at export it contained 28 admitted and 48 excluded runs, with 21 training and 7 validation raw sessions. It must be regenerated when current receipts change. `.state/representative-training-v4-final/` is a small, real three-run preprocessing check, with one training source session expanded to four orientations and two unaugmented validation sessions; it is not a complete training corpus and no model training was started. Its `source-index.json` binds the policy-v4 receipts, captures and split used at build time. The source validation must be repeated before using an old built dataset if admissions have changed. `docs/admitted-coverage-gaps-2026-10-28.md` records the separate raw/admitted and decoded-row limitations.

For the currently deployed worker and controller image identities, see the CURRENT section of [the reliability ledger](RELIABILITY-WORK-2026-10-28.md). Tags are mutable and must be re-inspected rather than treated as permanent identity.

## Reusable earned inventory context

`/opt/world-bank/inventory-sort-earned-world28` is a derived, read-only source template alongside the 50 original worlds. It is restricted to `mechanic-organize-inventory`, non-operator Survival, and uses the `inventory-sort-kit` gate. Its source is the independently admitted `reliability-organize-world28-02` starting world: the template's metadata and evidence sidecars bind the source run manifest, source admission receipt and exact starting world/metadata hashes. No source items were granted. Two isolated canaries with different run IDs and player UUIDs completed actual inventory sorting with goal-scoped clicks, matching before/after server inventory snapshots, item-count conservation, healthy ticks and independent admission. These establish the template's sorting behavior, not general item or mechanic coverage.

For each future assignment, the worker copies the verified bank world into its private workspace, migrates the source player's UUID and identity filenames only within that copy, verifies typed player NBT and nonidentity-file hashes, and writes `source-world-metadata.json` plus `earned-context-migration.json` into the run artifacts before server launch. The original bank and completed manifests remain unchanged. The scheduler, worker and admission/export checks require the source run's **current**, matching, policy-v4 eligible receipt under the central `.admission` directory; an embedded receipt is historical provenance and cannot override a current revocation. Current Docker workers mount the central dataset read-only at `/source-central` and use `/source-central/.admission` for admission checks; older workers can retain the previous `/source-admissions` mount until they finish. Kubernetes fails closed for earned templates unless a compatible read-only admission mount is available. Do not remove context gates or manually copy/generate inventory into a run.

The October 28 production browser check authenticated against the running controller and verified admitted 33/57 model-output and 17/30 input packet types, separate raw diagnostic counts, on-demand mechanic details, a real failure message and refreshed live inspection. Screenshots are private under `.state/gallery/final-browser-current/`. Those counts describe packet presence at that moment, not decoded rows or every HYBRID branch. Nether entry and the Gamer progression goal remain unproven; their bounded canary assignments are not successful outcomes merely because they are scheduled.

After the controller rollout, production run `20261028134505-af74a83432` completed `@get shield 1` on world12. Its central manifest verified and a fresh policy-v4 quality report admitted it with no exclusion reasons, client 20.005 TPS, server 20.001 TPS and zero degraded windows. The live eligible count rose from 28 to 29. This is one newly admitted run; it does not establish sustainable 15-worker capacity or complete packet coverage.

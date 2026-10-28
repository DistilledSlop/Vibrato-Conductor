'use strict';
// Runs only on an already isolated, verified world copy inside a worker.
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const zlib = require('zlib');
const assert = require('assert');
const nbt = require('/opt/proxy/node_modules/prismarine-nbt');

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const sha = b => crypto.createHash('sha256').update(b).digest('hex');
const ints = u => { const b = Buffer.from(u.replaceAll('-', ''), 'hex'); return [0, 4, 8, 12].map(i => b.readInt32BE(i)); };
const regular = file => { const s = fs.lstatSync(file); if (!s.isFile() || s.isSymbolicLink()) throw new Error('non-regular player file'); };

async function main() {
    const [world, from, to, proofJson] = process.argv.slice(2);
    const proof = JSON.parse(proofJson || "null");
    if (!world || !UUID.test(from || '') || !UUID.test(to || '') || from === to) throw new Error('invalid migration arguments');
    const oldInts = ints(from), newInts = ints(to);
    const records = [];
    let sourcePlayer = null;
    for (const suffix of ['.dat', '.dat_old']) {
        const source = path.join(world, 'playerdata', from + suffix);
        const target = path.join(world, 'playerdata', to + suffix);
        if (!fs.existsSync(source)) { if (suffix === '.dat') throw new Error('missing primary player data'); continue; }
        regular(source);
        if (fs.existsSync(target)) throw new Error('player target already exists');
        const original = fs.readFileSync(source);
        const {parsed} = await nbt.parse(original);
        if (!parsed.value || !parsed.value.UUID || parsed.value.UUID.type !== 'intArray') throw new Error('missing typed player UUID');
        assert.deepStrictEqual(Array.from(parsed.value.UUID.value), oldInts);
        if (suffix === '.dat') {
            const player = nbt.simplify(parsed);
            if (player.playerGameType !== 0 || !(player.Health > 0) || player.Dimension !== 'minecraft:overworld')
                throw new Error('source player is not a living overworld survivor');
            const stacks = player.Inventory || [];
            const inventory = {};
            for (const stack of stacks) inventory[stack.id] = (inventory[stack.id] || 0) + stack.count;
            sourcePlayer = {position: player.Pos, health: player.Health, dimension: player.Dimension,
                            gameType: player.playerGameType, inventory};
            const actual = stacks.filter(stack => stack.Slot === proof.sourceSlot);
            if (actual.length !== 1 || actual[0].id !== proof.item || actual[0].count !== proof.count
                    || stacks.some(stack => stack.Slot === proof.hotbarSlot))
                throw new Error('saved inventory does not match sort evidence');
        }
        parsed.value.UUID.value = newInts;
        const output = zlib.gzipSync(nbt.writeUncompressed(parsed, 'big'));
        const after = (await nbt.parse(output)).parsed;
        assert.deepStrictEqual(Array.from(after.value.UUID.value), newInts);
        after.value.UUID.value = oldInts;
        const initial = (await nbt.parse(original)).parsed;
        assert.deepStrictEqual(after, initial); // compare every type and value, not simplified NBT
        fs.writeFileSync(target, output, {flag: 'wx'});
        fs.unlinkSync(source);
        records.push({source: `playerdata/${from}${suffix}`, target: `playerdata/${to}${suffix}`,
                      sourceSha256: sha(original), targetSha256: sha(output), onlyUuidChanged: true});
    }
    for (const dir of ['stats', 'advancements']) {
        const source = path.join(world, dir, from + '.json');
        const target = path.join(world, dir, to + '.json');
        if (!fs.existsSync(source)) continue;
        regular(source);
        if (fs.existsSync(target)) throw new Error('identity target already exists');
        const bytes = fs.readFileSync(source);
        // Exclusive link avoids replacing a preexisting target even under a race.
        fs.linkSync(source, target); fs.unlinkSync(source);
        records.push({source: `${dir}/${from}.json`, target: `${dir}/${to}.json`, sha256: sha(bytes), onlyFilenameChanged: true});
    }
    process.stdout.write(JSON.stringify({sourceUuid: from, targetUuid: to, files: records, sourcePlayer, uuidOnlyGameplayStateUnchanged: true}));
}
main().catch(error => { console.error(error.stack || String(error)); process.exitCode = 1; });

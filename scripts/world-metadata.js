'use strict';
// Runs inside the pinned worker image; no host Node dependencies are required.
const fs = require('fs');
const path = require('path');
const nbt = require('/opt/proxy/node_modules/prismarine-nbt');
const seedString = value => Array.isArray(value) && value.length === 2
  ? ((BigInt(value[0]) << 32n) | BigInt(value[1] >>> 0)).toString() : String(value);
(async () => {
  const worlds = [];
  for (const entry of fs.readdirSync('/saves', { withFileTypes: true }).sort((a,b) => a.name.localeCompare(b.name, undefined, {numeric:true}))) {
    if (!entry.isDirectory()) continue;
    const root = path.join('/saves', entry.name), file = path.join(root, 'level.dat');
    if (!fs.existsSync(file)) continue;
    try {
      const {parsed} = await nbt.parse(fs.readFileSync(file));
      const data = nbt.simplify(parsed).Data;
      const dimensions = {};
      for (const [name, relative] of [['overworld','region'],['nether','DIM-1/region'],['end','DIM1/region']]) {
        const dir = path.join(root, relative); if (!fs.existsSync(dir)) continue;
        let count=0, minX=Infinity, maxX=-Infinity, minZ=Infinity, maxZ=-Infinity;
        for (const region of fs.readdirSync(dir)) {
          const match=/^r\.(-?\d+)\.(-?\d+)\.mca$/.exec(region); if(!match) continue;
          const fd=fs.openSync(path.join(dir,region),'r'), header=Buffer.alloc(4096);
          try { fs.readSync(fd,header,0,4096,0); } finally { fs.closeSync(fd); }
          for(let i=0;i<1024;i++) if(header.readUInt32BE(i*4)!==0) {
            count++; const x=+match[1]*32+i%32,z=+match[2]*32+Math.floor(i/32);
            minX=Math.min(minX,x); maxX=Math.max(maxX,x); minZ=Math.min(minZ,z); maxZ=Math.max(maxZ,z);
          }
        }
        dimensions[name]={storedChunks:count,chunkBounds:count?{minX,maxX,minZ,maxZ}:null};
      }
      worlds.push({name:entry.name,minecraftVersion:data.Version?.Name,dataVersion:data.DataVersion,
        sourceGameType:data.GameType,sourceAllowCommands:data.allowCommands,seed:seedString(data.WorldGenSettings?.seed),
        spawn:{x:data.SpawnX,y:data.SpawnY,z:data.SpawnZ},dimensions,dataPacks:data.DataPacks});
    } catch(error) { worlds.push({name:entry.name,error:error.message}); }
  }
  process.stdout.write(JSON.stringify({worlds},null,2)+'\n');
})().catch(error=>{console.error(error.message);process.exit(1);});

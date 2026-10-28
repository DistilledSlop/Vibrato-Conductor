'use strict';
const $ = id => document.getElementById(id);
const make = (tag, cls, text) => { const n=document.createElement(tag); if(cls)n.className=cls; if(text!==undefined)n.textContent=String(text); return n; };
const number = n => Number(n||0).toLocaleString();
const bytes = n => { n=Number(n||0); if(!n)return '0 B'; const u=['B','KiB','MiB','GiB','TiB']; const i=Math.min(4,Math.floor(Math.log(n)/Math.log(1024))); return `${(n/1024**i).toFixed(i?1:0)} ${u[i]}`; };
const age = wall => { const s=Math.max(0,Date.now()/1000-Number(wall||Date.now()/1000)); return s<60?`${Math.floor(s)}s` : s<3600?`${Math.floor(s/60)}m`:`${Math.floor(s/3600)}h ${Math.floor(s%3600/60)}m`; };
const activeStates=new Set(['pending','created','starting','running','healthy','unhealthy','stopping','missing']);
let firstSnapshot=true;
let state=null, filter='active', view='gallery', detailId=null, detailGeneration=0, polling=false, authenticated=false, coverageInventory=null;
const cardRefs=new Map();
const visible=new Set();
const observer=new IntersectionObserver(entries=>{ for(const entry of entries){ const id=entry.target.dataset.runId; if(entry.isIntersecting){visible.add(id);refreshThumbnail(id);}else visible.delete(id); } },{threshold:0.05});
async function api(path, options={}){
  const response=await fetch(path,{credentials:'same-origin',...options,headers:{'Content-Type':'application/json',...options.headers}});
  const result=await response.json();
  if(response.status===401){setAuthenticated(false);throw new Error(result.error||'Authentication required');}
  if(!response.ok)throw new Error(result.error||`Request failed (${response.status})`);
  return result;
}
function setAuthenticated(ok){authenticated=ok;$('loginPanel').hidden=ok;$('console').hidden=!ok;$('logout').hidden=!ok;$('connectionText').textContent=ok?'Live connection':'Console locked';$('connectionDot').classList.toggle('error',!ok);}
function toast(message,error=false){const n=$('toast');n.textContent=message;n.hidden=false;n.classList.toggle('error',error);clearTimeout(toast.timer);toast.timer=setTimeout(()=>n.hidden=true,4500);}
function pill(status){return make('span',`pill ${status||'neutral'}`,status==='running'?'Healthy':(status||'unknown').replaceAll('_',' '));}
function health(worker){return worker.liveHealth||worker.health||{};}
function heartbeat(worker){return worker.heartbeat||health(worker).heartbeat||{};}
function numeric(obj, keys){for(const key of keys){const val=obj?.[key];if(typeof val==='number'&&Number.isFinite(val))return val;}return null;}
function tps(worker){const h=health(worker),b=heartbeat(worker);let server=numeric(h,['serverMeanTps','serverTps']);if(server===null)server=numeric(h.server||h.serverHealth,['tps','meanTps']);if(server===null)server=numeric(b,['serverTps']);let client=numeric(h,['clientMeanTps','clientTps']);if(client===null)client=numeric(b,['tps']);return {server,client};}
function captureBytes(worker){const h=health(worker);return h.captureBytes??h.capture?.bytes??worker.result?.capture?.bytes??0;}
function workerTask(worker){return worker.task||worker.assignment?.task||'Unassigned';}
function taskSummary(worker){const hb=heartbeat(worker);if(worker.failureDetail)return worker.failureDetail;if(worker.reason)return worker.reason.replaceAll('_',' ');if(worker.result?.taskSuccess===true)return 'Assigned task completed';if(hb.scriptPhase==='post_task_settle')return 'Task completed · collecting final measurements';if(hb.leaf)return hb.leaf;if(hb.rootTask)return hb.rootTask;if(Number(hb.taskCompletedCount)>0)return 'No active task · task outcomes recorded';return 'Waiting for task telemetry';}
function shortId(id){return id.length>23?id.slice(0,11)+'…'+id.slice(-8):id;}
function selected(){return (state?.workers||[]).filter(w=>filter==='all'||(filter==='active'?activeStates.has(w.state):['unhealthy','failed','lost'].includes(w.state)));}
function updateState(next){
  state=next;setAuthenticated(true);
  if(firstSnapshot){firstSnapshot=false;if(!(state.counts?.active)&&state.workers?.length){filter='all';document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b.dataset.filter==='all'));}}
  const counts=state.counts||{};$('environment').textContent=(state.fleetId||'FLEET')+' / OPERATIONS';
  $('desired').textContent=number(state.desired);$('healthy').textContent=number(counts.healthy??counts.running);
  $('starting').textContent=number((counts.starting||0)+(counts.pending||0)+(counts.created||0));
  $('completed').textContent=number(counts.completed);$('failed').textContent=`${number((counts.failed||0)+(counts.lost||0))} failed runs retained`;
  const limit=state.capacity?.limit??state.config?.max_workers??0;$('capacity').textContent=state.config?.max_workers===0?`No count ceiling · storage capacity ${number(limit)}`:`Capacity ${number(limit)} workers`;
  if(document.activeElement!==$('target'))$('target').value=state.desired||0;if(state.config?.max_workers>0)$('target').max=state.config.max_workers;else $('target').removeAttribute('max');
  $('runtimeType').textContent=(state.backend==='kubernetes'?'Kubernetes':'Docker')+' runtime';
  $('subtitle').textContent=`${state.fleetId||'Default fleet'} · ${state.config?.mode||'diagnostic'} mode · Minecraft 1.21.1`;
  const error=state.controller?.error||state.controller?.deliveryError;const notice=error||state.pausedReason;
  $('resume').hidden=!['failure_circuit_open','failure_backoff'].includes(state.pausedReason);
  $('notice').hidden=!notice;$('notice').textContent=error?`Controller needs attention: ${error}`:notice?`New starts paused: ${notice.replaceAll('_',' ')}`:'';
  $('connectionDot').classList.toggle('error',!!error);$('connectionText').textContent=error?'Reconciler degraded':'Live connection';
  $('refreshed').textContent='Updated '+new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
  $('controllerStatus').textContent=state.controller?.lastReconcileWall?`Reconciled ${age(state.controller.lastReconcileWall)} ago`:'Reconciler starting';
  renderWorkers();renderCoverage();renderStorage();renderEvents();
}
function renderWorkers(){
  const workers=selected();$('workerCount').textContent=number(workers.length);$('empty').hidden=workers.length>0;
  $('empty').querySelector('h3').textContent=filter==='active'?'Ready when you are':'No matching workers';
  $('gallery').hidden=view!=='gallery'||!workers.length;$('tableView').hidden=view!=='table'||!workers.length;$('galleryNote').hidden=view!=='gallery';
  const keep=new Set(workers.map(w=>w.runId));for(const [id,ref] of cardRefs){if(!keep.has(id)){observer.unobserve(ref.card);ref.card.remove();cardRefs.delete(id);visible.delete(id);}}
  if(view==='gallery')for(const worker of workers){let ref=cardRefs.get(worker.runId);if(!ref){ref=createCard(worker);cardRefs.set(worker.runId,ref);$('gallery').append(ref.card);observer.observe(ref.card);}updateCard(ref,worker);}
  if(view==='table'){
    $('workers').replaceChildren();
    for(const w of workers){const tr=make('tr');const name=make('td');name.append(make('span','mono',shortId(w.runId)),make('span','sub',w.worldId||'Generated world'));const status=make('td');status.append(pill(w.state));const task=make('td');task.append(make('span','mono',workerTask(w)),make('span','sub',taskSummary(w)));const game=make('td');const ts=tps(w);game.append(make('span','mono',ts.server===null?'Server TPS pending':`${ts.server.toFixed(1)} server TPS`),make('span','sub',ts.client===null?'Client pending':`${ts.client.toFixed(1)} client TPS`));const capture=make('td');capture.append(make('span','mono',bytes(captureBytes(w))),make('span','sub',`${age(w.createdWall)} runtime`));const action=make('td');const b=make('button','secondary','Inspect ↗');b.addEventListener('click',()=>openWorker(w.runId));action.append(b);tr.append(name,status,task,game,capture,action);$('workers').append(tr);}
  }
}
function createCard(w){
  const card=make('article','worker-card');card.dataset.runId=w.runId;
  const screen=make('button','thumbnail');screen.setAttribute('aria-label',`Inspect worker ${w.runId}`);screen.addEventListener('click',()=>openWorker(w.runId));
  const no=make('div','no-screen');no.append(make('span','', '▧'),make('div','', 'Waiting for the client display'));
  const image=make('img');image.alt=`Live Minecraft view for ${w.runId}`;image.loading='eager';image.hidden=true;image.addEventListener('load',()=>{image.hidden=false;no.hidden=true;});image.addEventListener('error',()=>{image.hidden=true;no.hidden=false;});
  const overlay=make('div','overlay');const status=make('span');const stamp=make('span','', 'LIVE');overlay.append(status,stamp);screen.append(no,image,overlay);
  const body=make('div','card-body'),title=make('div','card-title'),name=make('span','card-name',shortId(w.runId)),runtime=make('span','pill neutral');title.append(name,runtime);
  const world=make('div','card-world'),task=make('div','card-task'),leaf=make('div','card-leaf'),metrics=make('div','card-metrics');body.append(title,world,task,leaf,metrics);card.append(screen,body);
  return {card,image,status,stamp,runtime,world,task,leaf,metrics,lastFrame:0,worker:w};
}
function updateCard(ref,w){
  ref.worker=w;ref.status.replaceChildren(pill(w.state));ref.runtime.textContent=age(w.createdWall);ref.world.textContent=(w.worldId||'Fresh generated world')+' · '+(w.gamemode||w.assignment?.gamemode||'survival')+(w.operator?' · operator':'');ref.task.textContent=workerTask(w);ref.leaf.textContent=taskSummary(w);
  const ts=tps(w);ref.metrics.replaceChildren();for(const [label,value] of [['TPS',ts.server===null?'—':ts.server.toFixed(1)],['CAPTURE',bytes(captureBytes(w))],['MEM',bytes(health(w).resources?.memoryBytes)]]){const n=make('span');n.append(make('span','',label+' '),make('strong','',value));ref.metrics.append(n);}
  if(w.screenshot&&visible.has(w.runId))refreshThumbnail(w.runId);
}
function refreshThumbnail(id,force=false){const ref=cardRefs.get(id);if(!ref||!ref.worker.screenshot||view!=='gallery'||!visible.has(id))return;const now=Date.now();if(!force&&now-ref.lastFrame<14500)return;ref.lastFrame=now;ref.image.src=`/api/artifacts/${encodeURIComponent(id)}/screenshot.png?t=${Math.floor(now/15000)}`;ref.stamp.textContent=activeStates.has(ref.worker.state)?'LIVE · 15s':'RETAINED FRAME';}
function renderCoverage(){
 const categories=state.coverage?.categories||{},model=state.coverage?.modelPacketCoverage||{};$('coverage').replaceChildren();
 const rows=[['Mechanics',categories.mechanic],['Model output packets',model.output],['Input packets',model.input],['Tasks',categories.task],['Items',categories.item],['Blocks',categories.block],['Mobs',categories.mob]];
 for(const [label,value] of rows){const c=value||{},row=make('div','coverage-row'),info=make('div','coverage-info'),bar=make('progress');info.append(make('span','',label),make('span','',`${number(c.observed)} / ${number(c.requested)}`));bar.max=Math.max(1,c.requested||1);bar.value=c.observed||0;bar.setAttribute('aria-label',label+' observed');row.append(info,bar);$('coverage').append(row);}
}
function renderMechanics(){
 const inventory=coverageInventory||{},model=inventory.modelPacketCoverage||{},query=$('mechanicSearch').value.toLowerCase(),names=new Map((model.targets||[]).map(t=>[t.id,t.readerName||t.label]));
 const tasks=(model.byTask||[]).filter(t=>(t.mechanics||[]).length&&(`${t.taskId} ${t.family} ${t.mechanics.join(' ')}`).toLowerCase().includes(query));$('mechanicList').replaceChildren();
 $('mechanicCounts').textContent=`${number(tasks.length)} focused assignments · ${number(model.output?.observed)} / ${number(model.output?.requested)} output packet types observed`;
 for(const task of tasks){const card=make('article','mechanic-card');card.append(make('h3','',task.taskId.replaceAll('_',' ')),make('p','',task.mechanics.join(' · ')));const seen=new Set(task.targetedObserved||[]);card.append(make('p','sub',`${number(seen.size)} / ${number(task.packetTargets?.length)} targeted types observed · ${number(task.packets?.length)} gameplay packet types recorded`));const details=make('details'),summary=make('summary','','Packet targets');details.append(summary);for(const id of task.packetTargets||[]){const row=make('div','gap-row');row.append(make('span','',names.get(id)||id),pill(seen.has(id)?'observed':'missing'));details.append(row);}card.append(details);$('mechanicList').append(card);}
 if(!tasks.length)$('mechanicList').append(make('p','','No matching focused assignments.'));
}

function renderStorage(){const s=state.storage||{};$('delivered').textContent=number(s.delivered);$('pending').textContent=bytes(Math.max(s.pendingBytes||0,s.undeliveredSpoolBytes||0));$('eligible').textContent=number(s.eligible);const n=$('storageStatus');n.replaceChildren(make('span',s.backpressure?'dot error':'dot'),document.createTextNode(s.backpressure?'Buffer full · new starts paused':s.delivered?'Verified central delivery':'Awaiting completed runs'));}
function renderEvents(){const events=(state.events||[]).slice(0,70);$('activityCount').textContent=number(events.length)+' events';$('events').replaceChildren();if(!events.length){$('events').append(make('div','empty-event','Fleet events will appear here.'));return;}for(const e of events){const row=make('div','event'),time=make('time','',new Date(e.wall*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})),body=make('div');body.append(make('p','',e.message||e.kind),make('small','',e.runId||e.kind));row.append(time,make('span',/fail|unhealthy|lost|error/.test(e.kind)?'dot error':'dot'),body);$('events').append(row);}}
async function poll(){if(polling)return;polling=true;try{updateState(await api('/api/state'));}catch(error){if(authenticated){$('connectionText').textContent='Connection interrupted';$('connectionDot').classList.add('error');$('notice').hidden=false;$('notice').textContent=error.message;}}finally{polling=false;}}
async function act(id,action){if(!confirm(action==='replace'?'Replace this worker with a fresh world? Its evidence will be retained.':'Stop this worker and reduce the fleet target? Its evidence will be retained.'))return;try{await api(`/api/workers/${encodeURIComponent(id)}/${action}`,{method:'POST',body:'{}'});toast(action==='replace'?'Replacement requested':'Worker stop requested');await poll();if(detailId===id)await refreshDetail();}catch(e){toast(e.message,true);}}
function detailMetric(label,value){const n=make('div','detail-metric');n.append(make('span','',label),make('strong','',value));return n;}
async function openWorker(id){detailId=id;detailGeneration++;$('detailTitle').textContent=id;$('workerDetail').replaceChildren(make('div','dialog-body','Loading worker evidence…'));$('workerDialog').showModal();await refreshDetail(true);}
async function refreshDetail(rebuild=false){
  const id=detailId,generation=detailGeneration;if(!id||!$('workerDialog').open)return;
  try{const w=await api('/api/workers/'+encodeURIComponent(id));if(generation!==detailGeneration)return;const root=$('workerDetail');
    if(rebuild||!$('detailFrame')){root.replaceChildren();const image=make('img','detail-image');image.id='detailFrame';image.alt='Minecraft client display';const controls=make('div','detail-controls');controls.append(make('span','', 'Live display · refreshes every 15 seconds'));for(const [action,label] of [['replace','Replace worker'],['stop','Stop worker']]){const b=make('button','secondary'+(action==='stop'?' danger':''),label);b.addEventListener('click',()=>act(id,action));controls.append(b);}const grid=make('div','detail-grid');grid.id='detailMetrics';const task=make('div','detail-task');task.id='detailTask';const tools=make('div','log-tools');tools.append(make('h2','', 'Recent process output'));const select=make('select');select.id='logComponent';select.setAttribute('aria-label','Log component');for(const name of ['client','server','proxy','display']){const option=make('option','',name);option.value=name;select.append(option);}select.addEventListener('change',()=>loadLogs(id));tools.append(select);const logs=make('pre');logs.id='logText';const links=make('div','artifact-links');for(const [path,label] of [['metadata.json','Run metadata'],['result.json','Result'],['manifest.json','Artifact manifest'],['health.json','Health evidence']]){const a=make('a','',label);a.href=`/api/artifacts/${encodeURIComponent(id)}/${path}`;a.target='_blank';a.rel='noopener';links.append(a);}root.append(image,controls,grid,task,tools,logs,links);}
    const displayLabel=root.querySelector('.detail-controls>span');if(displayLabel)displayLabel.textContent=activeStates.has(w.state)?'Live display · refreshes every 15 seconds':'Retained diagnostic frame · worker has stopped';
    const image=$('detailFrame');image.hidden=!w.screenshot;if(w.screenshot)image.src=`/api/artifacts/${encodeURIComponent(id)}/screenshot.png?t=${Math.floor(Date.now()/15000)}`;
    const ts=tps(w),hb=heartbeat(w);$('detailMetrics').replaceChildren(detailMetric('STATUS',w.state),detailMetric('SERVER TPS',ts.server===null?'Pending':ts.server.toFixed(2)),detailMetric('CLIENT TPS',ts.client===null?'Pending':ts.client.toFixed(2)),detailMetric('RAW CAPTURE',bytes(captureBytes(w))),detailMetric('MEMORY',bytes(health(w).resources?.memoryBytes)),detailMetric('CPU USED',Number(health(w).resources?.cpuUsageSeconds||0).toFixed(1)+' core-seconds'),detailMetric('DATASET',w.admission?(w.admission.eligible?'Eligible':'Excluded'):'Pending'));
    $('detailTask').replaceChildren(make('h2','',workerTask(w)),make('p','',taskSummary(w)),make('p','',`${w.worldId||'Generated world'} · seed ${w.seed} · ${hb.dimension||'dimension pending'} · ${age(w.createdWall)} runtime`));
    if(w.admission&&!w.admission.eligible)$('detailTask').append(make('p','', 'Dataset exclusion: '+(w.admission.reasons||[]).join(', ')));
    await loadLogs(id);
  }catch(e){if(generation===detailGeneration)toast(e.message,true);}
}
async function loadLogs(id){const component=$('logComponent')?.value||'client';try{const result=await api(`/api/workers/${encodeURIComponent(id)}/logs?component=${component}&lines=100`);if(detailId===id&&$('logText'))$('logText').textContent=result.text||'No process output yet.';}catch(e){if($('logText'))$('logText').textContent=e.message;}}
function renderGaps(){const coverage=coverageInventory||state?.coverage||{},query=$('gapSearch').value.toLowerCase();const targets=(coverage.targets||[]).filter(t=>(!t.observedRuns||!t.supported)&&(`${t.id} ${t.label}`).toLowerCase().includes(query));$('gapCounts').textContent=`${number((coverage.missing||[]).length)} unobserved targets · ${number((coverage.unsupported||[]).length)} explicitly unsupported targets`;$('gapList').replaceChildren();for(const t of targets.slice(0,200)){const row=make('div','gap-row'),label=make('span','',t.label||t.id);label.append(make('span','gap-kind',t.id));row.append(label,pill(t.supported?'missing':'unsupported'));$('gapList').append(row);}if(targets.length>200)$('gapList').append(make('p','',`${number(targets.length-200)} more targets. Refine the filter to inspect them.`));}
$('loginForm').addEventListener('submit',async e=>{e.preventDefault();$('loginError').textContent='';const token=$('token').value;try{await api('/api/session',{method:'POST',body:JSON.stringify({token})});$('token').value='';await poll();}catch(error){$('loginError').textContent=error.message;}});
$('logout').addEventListener('click',async()=>{await api('/api/logout',{method:'POST',body:'{}'});setAuthenticated(false);});
$('scaleForm').addEventListener('submit',async e=>{e.preventDefault();const button=e.submitter;button.disabled=true;try{await api('/api/fleet',{method:'POST',body:JSON.stringify({desired:Number($('target').value)})});toast('Fleet target updated');await poll();}catch(error){toast(error.message,true);}finally{button.disabled=false;}});
$('resume').addEventListener('click',async()=>{try{await api('/api/fleet/resume',{method:'POST',body:'{}'});toast('Backoff reset; resource and storage limits remain active');await poll();}catch(e){toast(e.message,true);}});
$('refresh').addEventListener('click',()=>{poll();for(const id of visible)refreshThumbnail(id,true);});
for(const button of document.querySelectorAll('[data-filter]'))button.addEventListener('click',()=>{filter=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b===button));renderWorkers();});
for(const button of document.querySelectorAll('[data-view]'))button.addEventListener('click',()=>{view=button.dataset.view;document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b===button));renderWorkers();});
for(const button of document.querySelectorAll('[data-close]'))button.addEventListener('click',()=>$(button.dataset.close).close());
$('workerDialog').addEventListener('close',()=>{detailId=null;detailGeneration++;});
$('mechanicDetails').addEventListener('click',async()=>{try{coverageInventory=await api('/api/coverage');renderMechanics();$('mechanicDialog').showModal();}catch(e){toast(e.message,true);}});$('mechanicSearch').addEventListener('input',renderMechanics);
$('coverageDetails').addEventListener('click',async()=>{try{coverageInventory=await api('/api/coverage');renderGaps();$('coverageDialog').showModal();}catch(e){toast(e.message,true);}});$('gapSearch').addEventListener('input',renderGaps);
setInterval(()=>{if(authenticated)poll();},4000);setInterval(()=>{if(authenticated){for(const id of visible)refreshThumbnail(id);refreshDetail();}},15000);poll();

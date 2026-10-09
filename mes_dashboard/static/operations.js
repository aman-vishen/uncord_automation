'use strict';
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = value => value == null ? '—' : Number(value).toLocaleString('en-IN');
const pct = value => value == null ? '—' : Number(value).toFixed(1).replace(/\.0$/, '') + '%';
const localTime = value => {
  if (!value) return 'No records';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? 'Unknown time' : date.toLocaleString('en-IN', {timeZone:'Asia/Kolkata',day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit',hour12:false});
};
const age = value => {
  if (!value) return 'No records';
  const stamp = typeof value === 'number' ? value * 1000 : new Date(value).getTime();
  const seconds = Math.max(0, Math.floor((Date.now() - stamp) / 1000));
  return seconds < 60 ? 'just now' : seconds < 3600 ? `${Math.floor(seconds / 60)}m ago` : seconds < 86400 ? `${Math.floor(seconds / 3600)}h ago` : `${Math.floor(seconds / 86400)}d ago`;
};
const badge = status => `<span class="badge ${['PASS','FAIL','ERROR'].includes(status) ? status : 'unknown'}"><span class="badge-mark" aria-hidden="true">${status === 'PASS' ? '✓' : status === 'FAIL' || status === 'ERROR' ? '!' : '·'}</span>${esc(status || 'No records')}</span>`;
const empty = (title, detail) => `<div class="empty-state"><span class="empty-icon" aria-hidden="true">◫</span><h3>${esc(title)}</h3><p>${esc(detail)}</p></div>`;
const views = {
  overview:['Production overview','A clear view of output, quality and the evidence behind every result.'],
  operations:['Production operations','Every supported operation, with attempt counts and first-pass evidence.'],
  quality:['Quality investigation','Separate first attempts, retests and latest product results.'],
  stations:['Station performance','Compare stations doing equivalent operations.'],
  records:['Production records','Search, filter and inspect the full recorded production history.'],
  passport:['Product passport','Follow a product through its recorded identity and operation history.'],
  health:['Data & synchronization','Understand freshness, inventory and the limits of reporting.']
};
let data = null, applied = {}, recordApplied = {}, page = 1, pages = 1;
let activeView = 'overview', summarySequence = 0, recordsSequence = 0, passportSequence = 0, eventSequence = 0;
let summaryController, recordsController, currentPassport = null, currentEvent = null;
let lastSuccess = null, connected = false, toastTimer;
function toast(message) { $('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 3000); }
function notice(message) { $('#notice').textContent = message; $('#notice').hidden = !message; }
function plantDate(date = new Date()) { const parts = new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(date); return ['year','month','day'].map(type => parts.find(p => p.type === type).value).join('-'); }
function preset(days) { const end = plantDate(), start = new Date(end + 'T12:00:00Z'); start.setUTCDate(start.getUTCDate() - days + 1); $('#start').value = start.toISOString().slice(0,10); $('#end').value = end; }
function formFilters() { return {start:$('#start').value,end:$('#end').value,model:$('#model').value,plant:$('#plant').value}; }
function recordFilters() { return {q:$('#recordSearch').value.trim(),stage:$('#recordStage').value,status:$('#recordStatus').value,station:$('#recordStation').value,sort:$('#recordSort').value,page_size:$('#pageSize').value,cohort:$('#recordCohort').value}; }
function params(filters) { return new URLSearchParams(Object.entries(filters).filter(([,value]) => value !== '' && value != null)).toString(); }
function syncURL() { const url = new URL(location.href); url.search = params({...applied,...recordApplied,...(activeView==='passport'&&currentPassport?.found?{product:$('#passportQuery').value}:{})}); url.hash = activeView; history.replaceState(null, '', url); }
function setView(view, sync = true) {
  const previousView = activeView;
  activeView = views[view] ? view : 'overview';
  $('#mobileView').value=activeView;
  document.querySelectorAll('.view').forEach(el => el.classList.toggle('active',el.id === activeView));
  document.querySelectorAll('nav button').forEach(el => { el.classList.toggle('active',el.dataset.view === activeView); if (el.dataset.view === activeView) el.setAttribute('aria-current','page'); else el.removeAttribute('aria-current'); });
  $('#pageTitle').textContent = views[activeView][0]; $('#pageDescription').textContent = views[activeView][1]; $('#breadcrumb').textContent = activeView === 'health' ? 'Data & sync' : activeView[0].toUpperCase()+activeView.slice(1);
  if (activeView === 'records') loadRecords();
  if(previousView!==activeView)window.scrollTo(0,0);
  if (sync) syncURL();
}
function populate(selector, options, allLabel) {
  const el = $(selector), previous = el.value;
  const signature = JSON.stringify(options.map(option=>typeof option==='string'?option:[option.key,option.label]));
  if(el.dataset.options===signature)return;
  el.dataset.options=signature;
  el.innerHTML = `<option value="">${esc(allLabel)}</option>` + options.map(option => `<option value="${esc(typeof option === 'string' ? option : option.key)}">${esc(typeof option === 'string' ? option : option.label)}</option>`).join('');
  if ([...el.options].some(option => option.value === previous)) el.value = previous;
  else if (previous) { const option = document.createElement('option'); option.value = previous; option.textContent = previous; el.append(option); el.value = previous; }
}
async function request(url, signal) { const response = await fetch(url,{signal,cache:'no-store'}); if (!response.ok) { let message = 'Unable to load data'; try { const error = await response.json(); message = error.error || message; } catch {} throw new Error(response.status === 401 ? 'Your session needs sign-in. Reload this page to authenticate.' : response.status === 400 ? message : `${message} (HTTP ${response.status}).`); } return response.json(); }
function connection(ok) { connected = ok; $('#connectionDot').className = 'dot ' + (ok ? 'good' : 'warning'); $('#connectionText').textContent = ok ? 'Dashboard connected' : 'Refresh unavailable'; $('#updatedAt').textContent = lastSuccess ? `Last refresh ${lastSuccess.toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit',hour12:false})} IST` : 'Waiting for data'; }
async function loadSummary(manual = false) {
  if (document.hidden && !manual) return;
  summaryController?.abort(); summaryController = new AbortController(); const sequence = ++summarySequence;
  $('#refreshButton').disabled = true;
  try {
    const result = await request('/api/operations?' + params(applied),summaryController.signal);
    if (sequence !== summarySequence) return;
    data = result; lastSuccess = new Date(); connection(true); notice(''); render(result); $('#kpis').setAttribute('aria-busy','false');
    if (activeView === 'records') loadRecords();
  } catch (error) { if (error.name !== 'AbortError' && sequence === summarySequence) { connection(false); notice(`${error.message} ${data ? 'Displayed figures are from the last successful refresh.' : 'No production figures have loaded.'}`); if (!data) $('#kpis').innerHTML = empty('Production data unavailable','Use Refresh after checking your connection or login.'); } }
  finally { if (sequence === summarySequence) $('#refreshButton').disabled = false; }
}
function applyFilters() {
  const selected = formFilters();
  if (!selected.start || !selected.end || selected.end < selected.start) { notice('Choose valid dates. To must be on or after From.'); return; }
  if ((new Date(selected.end) - new Date(selected.start)) / 86400000 > 366) { notice('Choose a period of 367 days or fewer.'); return; }
  applied = selected; page = 1; $('#filterDraft').hidden = true;
  document.querySelectorAll('[data-days]').forEach(button => { const days = (new Date(applied.end) - new Date(applied.start))/86400000 + 1; button.classList.toggle('selected',Number(button.dataset.days) === days && applied.end === plantDate()); });
  syncURL(); loadSummary(true);
}
function kpi(label,value,sub,definition,action,primary = false) { return `<article class="kpi ${primary ? 'primary-kpi' : ''}"><div class="label">${esc(label)}${definition ? `<button class="info-button" data-definition="${esc(definition)}" aria-label="Definition of ${esc(label)}">i</button>` : ''}</div>${action ? `<button class="kpi-link" data-kpi="${esc(action)}" aria-label="Explore ${esc(label)}">↗</button>` : ''}<div class="value">${value}</div><div class="sub">${sub}</div></article>`; }
function render(d) {
  populate('#model',d.options.models,'All models'); populate('#plant',d.options.plants,'All plants');
  populate('#recordStage',d.stages,'All operations'); populate('#stationOperation',d.stages,'All operations'); populate('#recordStation',d.options.stations,'All stations');
  $('#filterContext').textContent = `${d.range.start} → ${d.range.end} · ${applied.model || 'All models'} · ${applied.plant || 'All plants'} · Production dates in IST`;
  const m = d.metrics;
  $('#kpis').innerHTML = kpi('Good units',fmt(m.good_units),`${fmt(m.tested_units)} unique MAC-linked units tested`,'good_units','good',true) + kpi('Final first-pass yield',pct(m.final_first_pass_yield),`${fmt(m.first_passed)} passed / ${fmt(m.first_units)} first recorded tests`,'first_pass_yield','quality') + kpi('Latest failed units',fmt(m.latest_failed_units),'Latest result in selection is FAIL or ERROR','latest_failed','failed') + kpi('Recorded attempts',fmt(d.totals.attempts),`${fmt(d.totals.unlinked)} without a MAC link`,'attempts','records');
  $('#qualityKpis').innerHTML = kpi('Final first-pass yield',pct(m.final_first_pass_yield),`${fmt(m.first_units)} first recorded tests`,'first_pass_yield') + kpi('Units retested',fmt(m.retested_units),`${pct(m.retest_rate)} of MAC-linked units tested`,'retest_rate') + kpi('Additional final attempts',fmt(m.extra_attempts),'Additional attempts within this period','retest_rate') + kpi('All-operation pass rate',pct(m.attempt_pass_rate),`${fmt(d.totals.passed)} PASS / ${fmt(d.totals.attempts)} attempts`,'attempts');
  renderFreshness(d); chart('#outputChart',groupDays(d.daily),'good','attempts','Production by date');
  $('#attentionCount').textContent = d.attention.length;
  $('#attentionList').innerHTML = d.attention.length ? d.attention.map(item => `<div class="attention-item"><div class="attention-top"><span class="attention-icon" aria-hidden="true">${item.severity === 'warning' ? '!' : 'i'}</span><h3>${esc(item.title)}</h3></div><p>${esc(item.detail)}</p><button class="text-button" data-investigate="${esc(item.action)}" data-stage="${esc(item.stage || '')}" data-status="${item.stage ? 'FAILED' : ''}">Investigate →</button></div>`).join('') : empty('No flagged conditions','No rules were triggered by the available records. This is not a guarantee of complete factory coverage.');
  const operationCards = d.stages.map(operationCard).join(''); $('#operationPreview').innerHTML = operationCards; $('#allOperations').innerHTML = operationCards;
  renderOperations(d); renderQuality(d); renderStations(); renderRecent(d.recent); renderHealth(d);
}
function renderFreshness(d) {
  const f = d.freshness, stale = f.receipt_age_seconds == null || f.receipt_age_seconds > 900;
  $('#freshnessStrip').innerHTML = `<span class="freshness-item"><span class="dot ${stale ? 'warning' : 'good'}"></span><strong>${stale ? 'Receipt freshness needs context' : 'Recent cloud receipts'}</strong></span><span class="freshness-item">Received <strong title="${esc(localTime(f.last_received))}">${esc(age(f.last_received))}</strong></span><span class="freshness-item">Production <strong title="${esc(localTime(f.last_activity))}">${esc(age(f.last_activity))}</strong></span><span>Queue status <strong>Unknown</strong></span><button class="text-button" data-jump="health">Global data health ↗</button>`;
}
function operationCard(r) { return `<article class="operation-card ${r.attempts ? '' : 'no-data'}"><div class="operation-title"><h3>${esc(r.label)}</h3><button class="text-button" data-operation="${esc(r.key)}" aria-label="View ${esc(r.label)} records">↗</button></div><div class="attempts">${r.attempts ? fmt(r.attempts) + '<small>attempts</small>' : 'No records'}</div><div class="operation-result"><span class="green-text">${fmt(r.passed)} PASS</span><span class="red-text">${fmt(r.failed)} FAIL / ERROR</span><span>${pct(r.yield_rate)} pass rate</span></div><div class="bar-track"><div class="bar-fill" style="width:${r.yield_rate || 0}%"></div></div><div class="operation-meta">First-pass ${pct(r.first_pass_yield)} · ${fmt(r.first_units)} linked first tests</div></article>`; }
function renderOperations(d) { $('#operationTable').innerHTML = `<table><thead><tr><th>Operation</th><th>Attempts</th><th>PASS</th><th>FAIL + ERROR</th><th>Attempt pass rate</th><th>First-pass yield</th><th>First tests</th><th>Last activity · IST</th><th></th></tr></thead><tbody>${d.stages.map(r => `<tr><td>${esc(r.label)}</td><td class="number">${fmt(r.attempts)}</td><td class="number">${fmt(r.passed)}</td><td class="number">${fmt(r.failed)}</td><td>${pct(r.yield_rate)}</td><td>${pct(r.first_pass_yield)}</td><td class="number">${fmt(r.first_units)}</td><td>${esc(localTime(r.last_activity))}</td><td><button class="text-button" data-operation="${esc(r.key)}">Records →</button></td></tr>`).join('')}</tbody></table>`; }
function groupDays(rows) {
  if (rows.length <= 31) return rows.map(r => ({...r,label:r.day.slice(5),title:r.day}));
  const groups = new Map(), monthly = rows.length > 90;
  rows.forEach((r,i) => { const key = monthly ? r.day.slice(0,7) : Math.floor(i / 7); if (!groups.has(key)) groups.set(key,{label:monthly ? r.day.slice(0,7) : r.day.slice(5),title:monthly ? r.day.slice(0,7) : `Week from ${r.day}`,good:0,attempts:0}); const g = groups.get(key); g.good += r.good; g.attempts += r.attempts; });
  return [...groups.values()];
}
function chart(selector,rows,barKey,lineKey,title,target = 0) {
  if (!rows.length || !rows.some(r => r[barKey] || (lineKey && r[lineKey]))) { $(selector).innerHTML = empty('No reported activity','There are no qualifying records in this period. Check filters and data freshness.'); return; }
  const width = 720,height = 240,left = 44,right = 14,top = 15,bottom = 36,plotWidth = width-left-right,plotHeight = height-top-bottom;
  const max = Math.max(1,target,...rows.flatMap(r => [r[barKey] || 0,lineKey ? r[lineKey] || 0 : 0])), scaleMax = Math.ceil(max / 4) * 4;
  const x = i => left+(i+.5)*plotWidth/rows.length, y = value => top+plotHeight*(1-value/scaleMax), barWidth = Math.max(2,Math.min(28,plotWidth/rows.length*.55));
  const grid = Array.from({length:5},(_,i) => { const value = scaleMax*i/4; return `<line class="grid-line" x1="${left}" x2="${width-right}" y1="${y(value)}" y2="${y(value)}"/><text x="${left-8}" y="${y(value)+3}" text-anchor="end">${esc(fmt(value))}</text>`; }).join('');
  const bars = rows.map((r,i) => `<rect class="output-bar" x="${x(i)-barWidth/2}" y="${y(r[barKey] || 0)}" width="${barWidth}" height="${Math.max(0,y(0)-y(r[barKey] || 0))}" rx="3"><title>${esc(r.title || r.label)}: ${fmt(r[barKey])}${lineKey ? `; ${fmt(r[lineKey])} attempts` : ''}</title></rect>`).join('');
  const path = lineKey ? `<path class="attempt-path" d="${rows.map((r,i) => `${i ? 'L' : 'M'}${x(i)} ${y(r[lineKey] || 0)}`).join(' ')}"/>` : '';
  const interval = Math.max(1,Math.ceil(rows.length/9)), labels = rows.map((r,i) => i % interval === 0 || i === rows.length-1 ? `<text x="${x(i)}" y="${height-10}" text-anchor="middle">${esc(r.label)}</text>` : '').join('');
  const targetLine = target ? `<line class="target-line" x1="${left}" x2="${width-right}" y1="${y(target)}" y2="${y(target)}"/>` : '';
  $(selector).innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(title)}"><title>${esc(title)}</title><desc>${esc(rows.map(r => `${r.title || r.label}: ${r[barKey]}${lineKey ? ` good units and ${r[lineKey]} attempts` : ' attempts'}`).join('; '))}</desc>${grid}${bars}${path}${targetLine}${labels}</svg>`;
}
function renderQuality(d) {
  const failed = d.stages.filter(r => r.failed).sort((a,b) => b.failed-a.failed), max = Math.max(1,...failed.map(r => r.failed));
  $('#failureChart').innerHTML = failed.length ? failed.map(r => `<div class="metric-row"><button data-operation="${esc(r.key)}" data-status="FAILED">${esc(r.label)}<small>${fmt(r.attempts)} total attempts</small></button><div class="bar-track"><div class="bar-fill red" style="width:${r.failed/max*100}%"></div></div><strong class="metric-value">${fmt(r.failed)}</strong></div>`).join('') : empty('No failed attempts','No FAIL or ERROR records match the selected period.');
  $('#fpyChart').innerHTML = d.stages.map(r => `<div class="metric-row"><button data-operation="${esc(r.key)}">${esc(r.label)}<small>${fmt(r.first_units)} first recorded tests</small></button><div class="bar-track"><div class="bar-fill green" style="width:${r.first_pass_yield || 0}%"></div></div><strong class="metric-value">${pct(r.first_pass_yield)}</strong></div>`).join('');
  // Show a continuous 24-hour reporting window, ending at now or the historical selection end.
  let end = new Date(applied.end+'T23:00:00+05:30'); const now = new Date(); if (end > now) end = now;
  const endHour = new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Kolkata',hour:'2-digit',hourCycle:'h23'}).format(end);
  end = new Date(`${plantDate(end)}T${endHour}:00:00+05:30`);
  const startStamp = new Date(applied.start+'T00:00:00+05:30').getTime(), byHour = new Map(d.hourly.map(r => [r.hour,r]));
  const bins = [];
  for (let i=23;i>=0;i--) { const stamp = new Date(end.getTime()-i*3600000); if (stamp.getTime()<startStamp) continue; const date = plantDate(stamp), hour = new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Kolkata',hour:'2-digit',hourCycle:'h23'}).format(stamp); const key = `${date} ${hour}:00`; bins.push({attempts:byHour.get(key)?.attempts || 0,label:hour+':00',title:key+' IST'}); }
  const target = d.metrics.target_uph;
  $('#hourlyContext').textContent = 'Last 24 hours of selection, ending no later than now · final-verification attempts · IST';
  $('#hourlyTarget').textContent = target ? `Configured target: ${fmt(target)} units/h · attempts shown` : 'Hourly target not configured';
  chart('#hourlyChart',bins,'attempts',null,'Hourly final-verification attempts');
}
function renderStations() {
  if (!data) return;
  const operation = $('#stationOperation').value, rows = data.stations.filter(r => !operation || r.operation === operation);
  $('#stationTable').innerHTML = rows.length ? `<table><thead><tr><th>Station / plant</th><th>Operation</th><th>Attempts</th><th>PASS</th><th>FAIL + ERROR</th><th>Attempt pass rate</th><th>Last activity · IST</th><th></th></tr></thead><tbody>${rows.map(r => `<tr><td>${esc(r.station_id)}<span class="cell-secondary">${esc(r.plant_id)}</span></td><td>${esc(r.label)}</td><td class="number">${fmt(r.attempts)}</td><td class="number">${fmt(r.passed)}</td><td class="number">${fmt(r.failed)}</td><td>${pct(r.yield_rate)}</td><td>${esc(localTime(r.last_activity))}</td><td><button class="text-button" data-station="${esc(r.station_id)}" data-operation="${esc(r.operation)}">Records →</button></td></tr>`).join('')}</tbody></table>` : empty('No station activity','No recorded events match the current operation and production filters.');
}
function renderRecent(rows) { $('#recentActivity').innerHTML = rows.length ? rows.map(r => `<div class="activity-row">${badge(r.status)}<div class="activity-main"><button data-event="${esc(r.event_id)}"><strong>${esc(r.stage_label)}</strong></button><p>${esc(r.serial_number || r.mac || 'Unlinked record')} · ${esc(r.station_id)}</p></div><span class="activity-time">${esc(age(r.stamp))}</span></div>`).join('') : empty('No recent activity in this selection','Widen the date range or check the Data & Sync view.'); }
function renderHealth(d) {
  const f = d.freshness;
  const card = (label,value,description) => `<article class="health-card"><h3>${esc(label)}</h3><div class="health-value">${esc(value)}</div><p>${esc(description)}</p></article>`;
  $('#healthCards').innerHTML = card('Dashboard connection',connected ? 'Connected' : 'Unavailable','A successful query confirms MES access. Factory reporting is measured separately.') + card('Latest cloud receipt',age(f.last_received),localTime(f.last_received)+' · across all production records') + card('Latest production event',age(f.last_activity),localTime(f.last_activity)+' · original completion time') + card('Central Server queue','Not reported','Pending counts and retry errors require Central Server telemetry. Check the local Cloud MES Sync tab.') + card('Station connectivity','Not reported','Equipment heartbeats are not included in the current protocol. No activity can mean idle or disconnected.') + card('Unlinked attempts in selection',fmt(d.totals.unlinked),'Attempts without MAC identifiers are visible in records but excluded from MAC-linked unit metrics.');
  $('#inventoryTime').textContent = d.inventory.completed_at ? 'Snapshot '+localTime(d.inventory.completed_at) : 'No snapshot received';
  const inventory = d.inventory.metrics;
  $('#inventoryCards').innerHTML = [['Available','available'],['Reserved','reserved'],['Total','total'],['Used','used']].map(([label,key]) => kpi(label,fmt(inventory[key]),'Latest reported snapshot')).join('');
}
function applyRecordControls() { recordApplied = recordFilters(); page=1; syncURL(); loadRecords(); }
function openRecords(stage = '',status = '',station = '',cohort = '') { $('#recordStage').value = stage; $('#recordStatus').value = status; $('#recordStation').value = station; $('#recordCohort').value = cohort; $('#recordSearch').value = ''; recordApplied = recordFilters(); page=1; setView('records'); }
async function loadRecords() {
  recordsController?.abort(); recordsController = new AbortController(); const sequence = ++recordsSequence;
  const filters = {...applied,...recordApplied,page}; const scrollTop=$('#recordsTable').scrollTop,scrollLeft=$('#recordsTable').scrollLeft,focusedEvent=document.activeElement?.dataset.event;
  $('#recordsTable').setAttribute('aria-busy','true'); $('#exportRecords').href = '/export/records.csv?'+params({...applied,...recordApplied});
  try {
    const result = await request('/api/records?'+params(filters),recordsController.signal); if (sequence !== recordsSequence) return;
    page = result.page; pages=result.pages; $('#recordCount').textContent=fmt(result.total); $('#pageInfo').textContent = result.total ? `${fmt((page-1)*result.page_size+1)}–${fmt(Math.min(page*result.page_size,result.total))} of ${fmt(result.total)} matching records · page ${fmt(page)} / ${fmt(pages)}` : '0 matching records';
    $('#previousPage').disabled=page<=1; $('#nextPage').disabled=page>=pages;
    $('#recordsTable').innerHTML = result.rows.length ? `<table><thead><tr><th>Production time · IST</th><th>Product identifier</th><th>Model</th><th>Operation</th><th>Station</th><th>Result</th><th></th></tr></thead><tbody>${result.rows.map(r => `<tr><td class="record-time">${esc(localTime(r.stamp))}<span class="cell-secondary" title="${esc(localTime(r.receipt_stamp))}">Received ${esc(age(r.receipt_stamp))}</span></td><td class="record-id"><button class="text-button" data-passport="${esc(r.mac || r.serial_number || r.gpon_number || r.pcb_serial_number || '')}">${esc(r.serial_number || r.mac || r.gpon_number || r.pcb_serial_number || 'Unlinked')}</button><span class="cell-secondary">${esc(r.mac || 'No MAC link')}</span></td><td>${esc(r.model)}</td><td>${esc(r.stage_label)}</td><td>${esc(r.station_id)}<span class="cell-secondary">${esc(r.plant_id)}</span></td><td>${badge(r.status)}</td><td><button class="secondary" data-event="${esc(r.event_id)}">Details ↗</button></td></tr>`).join('')}</tbody></table>` : empty('No matching records','Try clearing the search or widening the production date range. Search applies to the full selected history.');
    $('#recordsTable').scrollTop=scrollTop;$('#recordsTable').scrollLeft=scrollLeft;
    if(focusedEvent){const button=[...$('#recordsTable').querySelectorAll('[data-event]')].find(el=>el.dataset.event===focusedEvent);button?.focus({preventScroll:true});}
  } catch(error) { if (error.name !== 'AbortError' && sequence === recordsSequence) { $('#recordsTable').innerHTML=empty('Records unavailable',error.message); $('#recordCount').textContent='—'; $('#pageInfo').textContent='Unable to load records'; $('#previousPage').disabled=true; $('#nextPage').disabled=true; } }
  finally { if(sequence === recordsSequence) $('#recordsTable').setAttribute('aria-busy','false'); }
}
async function showEvent(id) {
  const sequence=++eventSequence; currentEvent=null; $('#eventDetail').innerHTML=empty('Loading event…','Retrieving the source evidence.'); if (!$('#eventDialog').open) $('#eventDialog').showModal();
  try { const result=await request('/api/event?id='+encodeURIComponent(id)); if(sequence!==eventSequence)return; currentEvent=result.event; const r=currentEvent;
    const fields=[['Event ID',r.event_id],['Operation',r.stage_label],['Result',r.status],['Model',r.model],['Production time · IST',localTime(r.stamp)],['Cloud receipt · IST',localTime(r.receipt_stamp)],['Plant',r.plant_id],['Station',r.station_id],['MAC',r.mac],['Serial',r.serial_number],['GPON',r.gpon_number],['PCB serial',r.pcb_serial_number],['Firmware',r.firmware_version],['Source log',r.source_file]];
    $('#eventDetail').innerHTML=`<div class="detail-grid">${fields.map(([label,value])=>`<div><span>${esc(label)}</span>${esc(value || 'Not reported')}</div>`).join('')}</div><div class="dialog-actions"><button class="primary" data-passport="${esc(r.mac || r.serial_number || r.gpon_number || r.pcb_serial_number || '')}">Open product passport →</button><button class="secondary" data-copy="${esc(r.event_id)}">Copy event ID</button></div><details open><summary>Result detail</summary><pre>${esc(r.detail || 'No detail reported')}</pre></details><details><summary>Raw source log</summary><pre>${esc(r.raw_log || 'No raw log reported')}</pre></details><details><summary>Original payload</summary><pre>${esc(JSON.stringify(r.payload,null,2))}</pre></details>`;
  } catch(error){if(sequence===eventSequence)$('#eventDetail').innerHTML=empty('Unable to open event',error.message);}
}
async function lookupPassport(query,mac='',plant='') {
  if(!query.trim())return; const sequence=++passportSequence; $('#passportQuery').value=query; setView('passport'); $('#passportResult').innerHTML=empty('Looking up product…','Searching all recorded dates.');
  try { const result=await request('/api/passport?'+params({q:query,mac,plant})); if(sequence!==passportSequence)return; currentPassport=result;
    if(!result.found){$('#passportResult').innerHTML=empty('No matching product','Check the identifier. The product may not have uploaded, or its identity may not be linked.');return;}
    if(result.ambiguous){$('#passportResult').innerHTML=`<article class="panel"><h2>Choose the product</h2><p class="footnote">This identifier matches more than one plant or MAC link. No product has been selected automatically.</p>${result.candidates.map(r=>`<div class="candidate"><div><strong>${esc(r.unit_key || 'No MAC link')}</strong><span class="cell-secondary">${esc(r.plant_id)} · ${esc(localTime(r.latest))}</span></div>${r.unit_key?`<button class="secondary" data-candidate="${esc(r.unit_key)}" data-plant="${esc(r.plant_id)}">Open →</button>`:'<span class="tag">Identity link needed</span>'}</div>`).join('')}${result.truncated?'<p class="footnote">More than 50 matches. Use a more specific identifier.</p>':''}</article>`;return;}
    const identity=result.identity, latestFinal=result.operations.find(r=>r.key==='QUALITY_VERIFICATION_RESULT')?.latest;
    $('#passportResult').innerHTML=`<article class="panel"><div class="passport-header"><div><span class="section-kicker">RECORDED PRODUCT IDENTITY</span><h2>${esc(identity.serial_number || identity.mac || query)}</h2><p class="footnote">${esc(identity.model)} · ${esc(identity.plant_id)} · ${fmt(result.total)} recorded events · latest final result ${latestFinal ? esc(latestFinal.status) : 'not recorded'}</p></div><button class="secondary" data-print>Print passport ↓</button></div><div class="identity-grid">${[['MAC',identity.mac],['Serial',identity.serial_number],['GPON',identity.gpon_number],['PCB serial',identity.pcb_serial_number],['Model',identity.model],['Plant',identity.plant_id]].map(([label,value])=>`<div class="identity-cell"><span>${esc(label)}</span><strong>${esc(value || 'Not reported')}</strong>${value?`<button class="copy-button" data-copy="${esc(value)}" aria-label="Copy ${esc(label)}">Copy</button>`:''}</div>`).join('')}</div>${!result.identity_linked?'<p class="notice">No MAC link: this view only joins directly matching identifiers. A complete product history cannot be confirmed.</p>':''}<p class="footnote">Latest known values. No configured route or shipment state is inferred. Operations with no event are unknown, not confirmed skipped.</p></article><article class="panel"><div class="panel-head"><div><h2>Latest operation evidence</h2><p>Latest recorded result per operation · all dates</p></div></div><div class="passport-operations">${result.operations.map(r=>`<div class="passport-stage"><strong>${esc(r.label)}</strong>${badge(r.latest?.status)}<small>${r.latest?esc(localTime(r.latest.stamp)):'No recorded evidence'}</small>${r.latest?`<button class="text-button" data-event="${esc(r.latest.event_id)}">View evidence →</button>`:''}</div>`).join('')}</div></article><article class="panel"><div class="panel-head"><div><h2>Production timeline</h2><p>Newest first · production time in IST</p></div><span class="tag">${fmt(result.total)} events</span></div>${result.truncated?'<p class="notice">Showing the latest 1,000 events. Use Production Records to search or export more history.</p>':''}<div class="timeline">${result.history.map(r=>`<div class="timeline-row"><div class="timeline-time">${esc(localTime(r.stamp))}</div><div class="timeline-rail"><span class="timeline-dot"></span></div><div class="timeline-content"><h3>${esc(r.stage_label)} ${badge(r.status)}</h3><p>${esc(r.station_id)} · ${esc(r.detail || 'No result detail reported')}</p><button class="text-button" data-event="${esc(r.event_id)}">Inspect event →</button></div></div>`).join('')}</div></article>`;
    const url=new URL(location.href);url.searchParams.set('product',query);history.replaceState(null,'',url);
  }catch(error){if(sequence===passportSequence)$('#passportResult').innerHTML=empty('Product lookup unavailable',error.message);}
}
document.addEventListener('click',event=>{
  const button=event.target.closest('button,a'); if(!button)return;
  if(button.dataset.view)setView(button.dataset.view);
  if(button.dataset.jump)setView(button.dataset.jump);
  if(button.dataset.operation)openRecords(button.dataset.operation,button.dataset.status || '',button.dataset.station || '');
  if(button.dataset.investigate){ if(button.dataset.investigate==='records')openRecords(button.dataset.stage || '',button.dataset.status || '');else setView(button.dataset.investigate); }
  if(button.dataset.event)showEvent(button.dataset.event);
  if('passport' in button.dataset){ if(!button.dataset.passport){toast('This event has no searchable product identifier.');return;}$('#eventDialog').close();lookupPassport(button.dataset.passport); }
  if(button.dataset.candidate)lookupPassport($('#passportQuery').value,button.dataset.candidate,button.dataset.plant);
  if(button.dataset.definition){const key=button.dataset.definition;const message=data?.definitions[key] || ({attempts:'All reported production events in this period, including failures and retests. Unit counts require MAC identity.',latest_failed:'MAC-linked products whose latest final-verification attempt inside this selection is FAIL or ERROR.'}[key]);$('#eventDetail').innerHTML=`<p class="definition-list">${esc(message)}</p>`;$('#eventDialog').showModal();}
  if(button.dataset.kpi){const action=button.dataset.kpi;if(action==='quality')setView('quality');else if(action==='good')openRecords('QUALITY_VERIFICATION_RESULT','','','latest_good');else if(action==='failed')openRecords('QUALITY_VERIFICATION_RESULT','','','latest_failed');else openRecords();}
  if(button.hasAttribute('data-print'))window.print();
  if(button.dataset.copy)navigator.clipboard.writeText(button.dataset.copy).then(()=>toast('Copied to clipboard')).catch(()=>toast('Clipboard unavailable. Select the identifier to copy it.'));
});
$('#filterForm').addEventListener('submit',event=>{event.preventDefault();applyFilters();});
$('#filterForm').addEventListener('input',()=>{$('#filterDraft').hidden=false;});
document.querySelectorAll('[data-days]').forEach(button=>button.addEventListener('click',()=>{preset(Number(button.dataset.days));applyFilters();}));
$('#resetFilters').addEventListener('click',()=>{preset(7);$('#model').value='';$('#plant').value='';applyFilters();});
$('#refreshButton').addEventListener('click',()=>loadSummary(true));
$('#recordForm').addEventListener('submit',event=>{event.preventDefault();applyRecordControls();});
['#recordStage','#recordStatus','#recordStation','#recordSort','#pageSize','#recordCohort'].forEach(selector=>$(selector).addEventListener('change',applyRecordControls));
$('#clearRecords').addEventListener('click',()=>openRecords());
document.querySelectorAll('[data-saved]').forEach(button=>button.addEventListener('click',()=>openRecords('',button.dataset.saved==='failed'?'FAILED':'')));
$('#previousPage').addEventListener('click',()=>{if(page>1){page--;loadRecords();}});$('#nextPage').addEventListener('click',()=>{if(page<pages){page++;loadRecords();}});
$('#densityButton').addEventListener('click',()=>{const compact=$('#recordsTable').classList.toggle('compact');$('#densityButton').textContent=compact?'Comfortable rows':'Compact rows';});
$('#stationOperation').addEventListener('change',renderStations);
$('#mobileView').addEventListener('change',()=>setView($('#mobileView').value));
$('#passportForm').addEventListener('submit',event=>{event.preventDefault();lookupPassport($('#passportQuery').value.trim());});
$('#closeDialog').addEventListener('click',()=>$('#eventDialog').close());
$('#eventDialog').addEventListener('click',event=>{if(event.target===$('#eventDialog')){const rect=$('#eventDialog').getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)$('#eventDialog').close();}});
function wallboard(enabled){document.body.classList.toggle('wallboard',enabled);$('#exitWallboard').hidden=!enabled;if(enabled)setView('overview');}
$('#wallboardButton').addEventListener('click',()=>wallboard(true));$('#exitWallboard').addEventListener('click',()=>wallboard(false));document.addEventListener('keydown',event=>{if(event.key==='Escape')wallboard(false);});
function theme(dark){document.documentElement.dataset.theme=dark?'dark':'light';$('#themeButton').setAttribute('aria-label',dark?'Switch to light appearance':'Switch to dark appearance');try{localStorage.setItem('ete-mes-theme',dark?'dark':'light');}catch{}}
$('#themeButton').addEventListener('click',()=>theme(document.documentElement.dataset.theme!=='dark'));
try{if(localStorage.getItem('ete-mes-theme')==='dark')theme(true);}catch{}
const initial=new URLSearchParams(location.search);
for(const key of ['start','end','model','plant']){const value=initial.get(key);if(!value)continue;const el=$('#'+key);if(el.tagName==='SELECT'){const option=document.createElement('option');option.value=value;option.textContent=value;el.append(option);}el.value=value;}
for(const [key,selector] of Object.entries({q:'#recordSearch',stage:'#recordStage',status:'#recordStatus',station:'#recordStation',sort:'#recordSort',page_size:'#pageSize',cohort:'#recordCohort'})){const value=initial.get(key);if(!value)continue;const el=$(selector);if(el.tagName==='SELECT'&&![...el.options].some(o=>o.value===value)){const option=document.createElement('option');option.value=value;option.textContent=value;el.append(option);}el.value=value;}
applied=formFilters();recordApplied=recordFilters();setView(location.hash.slice(1)||'overview',false);loadSummary(true);
if(initial.get('product'))lookupPassport(initial.get('product'));
window.addEventListener('hashchange',()=>setView(location.hash.slice(1)||'overview'));
document.addEventListener('visibilitychange',()=>{if(!document.hidden)loadSummary();});
setInterval(()=>loadSummary(),Math.max(5,window.MES_REFRESH||10)*1000);

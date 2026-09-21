let state={recent:[],models:[]};
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
function params(){const m=$('#model').value;return `start=${$('#start').value}&end=${$('#end').value}${m?'&model='+encodeURIComponent(m):''}`}
function badge(v){v=v||'-';return `<span class="badge ${esc(v)}">${esc(v)}</span>`}
function bareStage(v){return String(v||'').replace(/^\d+\.\s*/,'')}
function pct(v){return Number(v||0).toFixed(1).replace('.0','')}
async function load(){
  try{
    let r=await fetch('/api/dashboard?'+params());
    if(!r.ok)throw new Error('HTTP '+r.status);
    let d=await r.json();state=d;syncModels(d);render(d);
    $('#healthDot').style.background='#1f9d62';$('#healthText').textContent='MES online'
  }catch(e){
    $('#healthDot').style.background='#d84949';$('#healthText').textContent='MES offline'
  }
}
function syncModels(d){
  const sel=$('#model'),current=sel.value,models=(d.filters&&d.filters.models)||[];
  const html=['<option value="">All models</option>',...models.map(m=>`<option value="${esc(m)}">${esc(m)}</option>`)].join('');
  if(sel.dataset.models!==JSON.stringify(models)){sel.innerHTML=html;sel.dataset.models=JSON.stringify(models);sel.value=current||((d.filters&&d.filters.model)||'')}
}
function render(d){
  const k=d.kpi;
  const uphSub=k.target_uph?(`Target ${k.target_uph} UPH`):'Set TARGET_UPH to compare';
  let cards=[
    ['Line Input',k.line_input,'Wi-Fi Calibration tested'],
    ['Finished Output',k.production_volume,'Final Verification tested'],
    ['Final FPY',pct(k.final_yield)+'%',`${k.final_pass} PASS / ${k.final_fail} FAIL`],
    ['Current UPH',k.current_uph,uphSub],
    ['Estimated WIP',k.wip,'Line input minus finished output'],
    ['Retests',k.retest_count,`${pct(k.retest_rate)}% of unique finished MACs`],
    ['MAC Write FPY',pct(k.writer_yield)+'%',`${k.writer_pass} PASS / ${k.writer_fail} FAIL`],
    ['Available MACs',k.available_macs,`${k.reserved_macs} currently reserved`]
  ];
  $('#kpis').innerHTML=cards.map(x=>`<div class="kpi"><div class="label">${x[0]}</div><div class="value">${x[1]}</div><div class="sub">${x[2]}</div></div>`).join('');
  $('#rangeText').textContent=`${d.range.start} to ${d.range.end}`;
  $('#uphTargetText').textContent=k.target_uph?`Target: ${k.target_uph} UPH`:'TARGET_UPH not configured';
  renderProcess(d.stages);renderDaily(d.daily);renderYield(k.final_yield,k.final_pass,k.final_fail);
  renderStages(d.stages);renderStations(d.stations);renderRecords(d.recent);
  renderHourly(d.hourly,k.target_uph);renderPareto(d.failure_pareto);renderFunnel(d.funnel);
  renderInsights(d.insights);renderStationCompare(d.stations);
  $('#export').href='/export/production.csv?'+params();
}
function renderProcess(rows){
  $('#processFlow').innerHTML=rows.map((r,i)=>`<div class="process-step ${r.optional?'optional':''}"><div class="step-no">${i+1}</div><div class="step-name">${esc(bareStage(r.stage))}</div><div class="step-total">${r.tested}</div><div class="step-results"><span class="pass-text">${r.pass} PASS</span><span class="fail-text">${r.fail} FAIL</span></div><div class="track"><div class="fill" style="width:${r.yield}%"></div></div><strong>${pct(r.yield)}%${r.optional?' *':''}</strong></div>${i<rows.length-1?'<div class="process-arrow">›</div>':''}`).join('')
}
function renderDaily(rows){
  let max=Math.max(1,...rows.map(r=>r.total));
  $('#dailyChart').innerHTML=rows.length?rows.map(r=>`<div class="day"><div class="bar pass" title="PASS ${r.pass}" style="height:${r.pass/max*100}%"></div><div class="bar fail" title="FAIL ${r.fail}" style="height:${r.fail/max*100}%"></div><label>${esc(r.day.slice(5))}</label></div>`).join(''):'<div class="muted">No finished production data in this date range.</div>'
}
function renderHourly(rows,target){
  const recent=(rows||[]).slice(-24),max=Math.max(1,target||0,...recent.map(r=>r.total));
  $('#hourlyChart').innerHTML=recent.length?recent.map(r=>`<div class="day"><div class="bar pass" title="${esc(r.hour)}: ${r.total} units" style="height:${r.total/max*100}%"></div><label>${esc(r.hour.slice(11,13))}:00</label></div>`).join(''):'<div class="muted">No hourly output data.</div>'
}
function renderYield(y,p,f){
  $('#yieldRing').innerHTML=`<div class="ring" style="background:conic-gradient(#1e84c6 ${y}%,#e5edf3 0)"><div class="inside"><strong>${pct(y)}%</strong><small>${p} PASS / ${f} FAIL</small></div></div>`
}
function renderStages(rows){
  $('#stageOverview').innerHTML=rows.map(r=>`<div class="stage-row"><span>${esc(r.stage)}${r.optional?' *':''}</span><div class="track"><div class="fill" style="width:${r.yield}%"></div></div><strong>${pct(r.yield)}%</strong></div>`).join('');
  $('#stageTable').innerHTML=rows.map(r=>`<tr><td>${esc(r.stage)}${r.optional?' <span class="optional-tag">model-dependent</span>':''}</td><td>${r.tested}</td><td>${r.pass}</td><td>${r.fail}</td><td><strong>${pct(r.yield)}%</strong></td><td><div class="track"><div class="fill" style="width:${r.yield}%"></div></div></td></tr>`).join('')
}
function renderPareto(rows){
  const max=Math.max(1,...(rows||[]).map(r=>r.fail));
  $('#pareto').innerHTML=(rows||[]).length?rows.map((r,i)=>`<div class="metric-bar"><span class="rank">${i+1}</span><span class="metric-name">${esc(r.stage)}</span><div class="track"><div class="fill fail-fill" style="width:${r.fail/max*100}%"></div></div><strong>${r.fail}</strong></div>`).join(''):'<div class="muted">No failures in the selected range.</div>'
}
function renderFunnel(rows){
  const max=Math.max(1,...(rows||[]).map(r=>r.count));
  $('#funnel').innerHTML=(rows||[]).map(r=>`<div class="funnel-row"><span>${esc(r.stage)}${r.optional?' *':''}</span><div class="funnel-bar" style="width:${Math.max(8,r.count/max*100)}%">${r.count}</div></div>`).join('')
}
function renderInsights(rows){
  $('#insights').innerHTML=(rows||[]).map(r=>`<div class="insight ${esc(r.level||'info')}"><strong>${esc(r.title)}</strong><p>${esc(r.text)}</p></div>`).join('')
}
function renderStations(rows){
  $('#stationTable').innerHTML=rows.length?rows.map(r=>`<tr><td>${esc(r.client_id)}</td><td>${r.total}</td><td>${r.pass}</td><td>${r.fail}</td><td><strong>${pct(r.yield)}%</strong></td><td>${esc(r.last_result||'-')}</td></tr>`).join(''):'<tr><td colspan="6" class="muted">No station data.</td></tr>'
}
function renderStationCompare(rows){
  const max=Math.max(1,...(rows||[]).map(r=>r.total));
  $('#stationCompare').innerHTML=(rows||[]).length?rows.slice(0,20).map(r=>`<div class="station-bar"><div><strong>${esc(r.client_id)}</strong><small>${r.total} tests · ${pct(r.yield)}% FPY</small></div><div class="track"><div class="fill" style="width:${r.total/max*100}%"></div></div></div>`).join(''):'<div class="muted">No station data.</div>'
}
function renderRecords(rows){state.recent=rows;filterRecords()}
function shortFile(v){if(!v)return '-';return String(v).split(/[\\/]/).pop()}
function filterRecords(){
  let q=$('#search').value.toLowerCase();
  let rows=(state.recent||[]).filter(r=>Object.values(r).join(' ').toLowerCase().includes(q));
  $('#recordsTable').innerHTML=rows.length?rows.map(r=>`<tr><td>${esc(r.completed_at||'-')}</td><td>${esc(r.stage||'-')}</td><td>${esc(r.model||'-')}</td><td>${esc(r.mac||'-')}</td><td>${esc(r.serial_number||'-')}</td><td>${esc(r.gpon_number||'-')}</td><td>${esc(r.pcb_serial_number||'-')}</td><td>${esc(r.client_id||'-')}</td><td>${esc(r.router_ip||'-')}</td><td>${esc(r.firmware_version||r.firmware_result||'-')}</td><td title="${esc(r.source_file||'')}">${esc(shortFile(r.source_file))}</td><td>${badge(r.status)}</td><td class="detail-cell" title="${esc(r.detail||'')}">${esc((r.detail||'-').slice(0,100))}</td></tr>`).join(''):'<tr><td colspan="13" class="muted">No records in this date range.</td></tr>'
}
document.querySelectorAll('nav button').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('nav button,.view').forEach(x=>x.classList.remove('active'));b.classList.add('active');$('#'+b.dataset.view).classList.add('active')}));
$('#apply').addEventListener('click',load);$('#search').addEventListener('input',filterRecords);$('#model').addEventListener('change',load);
load();setInterval(load,Math.max(5,window.MES_REFRESH||10)*1000);

async function traceSearch(){
  let q=$('#traceQuery').value.trim();if(!q)return;
  $('#traceTable').innerHTML='<tr><td colspan="9" class="muted">Searching…</td></tr>';
  try{
    let r=await fetch('/api/traceability?q='+encodeURIComponent(q));let d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||('HTTP '+r.status));
    if(!d.found){$('#traceIdentity').innerHTML='';$('#traceTable').innerHTML='<tr><td colspan="9" class="muted">No matching production identity found.</td></tr>';return}
    let i=d.identity||{};
    $('#traceIdentity').innerHTML=[['MAC',i.mac],['Serial',i.serial_number],['GPON',i.gpon_number],['PCB Serial',i.pcb_serial_number]].map(x=>`<div class="kpi"><div class="label">${esc(x[0])}</div><div class="value trace-value">${esc(x[1]||'-')}</div></div>`).join('');
    $('#traceTable').innerHTML=(d.history||[]).map(x=>`<tr><td>${esc(x.completed_at||'-')}</td><td>${esc(x.stage||'-')}</td><td>${esc(x.mac||'-')}</td><td>${esc(x.serial_number||'-')}</td><td>${esc(x.gpon_number||'-')}</td><td>${esc(x.pcb_serial_number||'-')}</td><td>${esc(x.station_id||'-')}</td><td>${badge(x.status)}</td><td class="detail-cell">${esc(x.detail||'-')}</td></tr>`).join('')||'<tr><td colspan="9" class="muted">No history found.</td></tr>'
  }catch(e){$('#traceTable').innerHTML=`<tr><td colspan="9" class="muted">${esc(e.message)}</td></tr>`}
}
$('#traceSearch').addEventListener('click',traceSearch);$('#traceQuery').addEventListener('keydown',e=>{if(e.key==='Enter')traceSearch()});

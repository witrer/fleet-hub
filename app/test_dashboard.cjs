// Targeted dashboard behavior checks. Run: node --test app/test_dashboard.cjs
// No browser, server, network, credentials, or extra npm dependencies are required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(path.join(__dirname, 'static/index.html'), 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];

function harness() {
  const elements = new Map(), intervals = [], all = [], requests = [], timers = new Map();
  let nextTimer = 0;
  let now = 1700010321000, fetcher, regionConstructions = 0;
  class Element {
    constructor(tag='div') {
      this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.attributes = {};
      this.className = ''; this.value = ''; this.hidden = false; this.listeners = {}; this.replacements = 0;
      this.style = {setProperty(){}}; this._text = '';
      this.classList = {
        toggle: (name, on) => {const names = new Set(this.className.split(/\s+/).filter(Boolean)); if(on===undefined)on=!names.has(name); if(on)names.add(name);else names.delete(name);this.className=[...names].join(' ');},
        add: name => this.classList.toggle(name,true), remove: name => this.classList.toggle(name,false),
      };
      all.push(this);
    }
    get textContent() {return this._text+this.children.map(child=>child.textContent).join('');}
    set textContent(value) {this._text=String(value);this.children=[];}
    get options() {return this.children;}
    append(...children) {for(let child of children){if(typeof child==='string'){const text=new Element('#text');text.textContent=child;child=text;}if(child.parent)child.parent.children=child.parent.children.filter(x=>x!==child);child.parent=this;this.children.push(child);}}
    replaceChildren(...children) {this.replacements++;for(const child of this.children)child.parent=null;this.children=[];this._text='';this.append(...children);}
    setAttribute(key,value) {this.attributes[key]=String(value);}
    removeAttribute(key) {delete this.attributes[key];}
    addEventListener(name,fn) {(this.listeners[name]??=[]).push(fn);}
    showModal() {this.open=true;}
    close() {this.open=false;}
    focus() {document.activeElement=this;}
    querySelectorAll(selector) {return descendants(this).filter(node=>matches(node,selector));}
  }
  function descendants(root) {return root.children.flatMap(child=>[child,...descendants(child)]);}
  function matches(node,selector) {
    if(selector==='button')return node.tagName==='BUTTON';
    if(selector==='.node-menu[open]')return node.className.split(' ').includes('node-menu')&&node.open;
    if(selector==='.node-menu')return node.className.split(' ').includes('node-menu');
    if(selector==='[aria-describedby="latency-tooltip"]')return node.attributes['aria-describedby']==='latency-tooltip';
    const data=selector.match(/^\[data-([\w-]+)\]$/);
    return data ? data[1].replace(/-([a-z])/g,(_,c)=>c.toUpperCase()) in node.dataset : false;
  }
  const document = {
    hidden:false, activeElement:null, listeners:{},
    getElementById(id){if(!elements.has(id)){const element=new Element();element.id=id;elements.set(id,element);}return elements.get(id);},
    createElement: tag=>new Element(tag),
    createElementNS: (namespace,tag)=>{const element=new Element(tag);element.namespaceURI=namespace;return element;},
    createTextNode: text=>{const element=new Element('#text');element.textContent=text;return element;},
    querySelectorAll(selector){return all.filter(node=>matches(node,selector));},
    addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);},
  };
  for(const name of ['overview','manage','alerts','settings']){const button=new Element('button');button.dataset.page=name;const pane=new Element('section');pane.dataset.pane=name;}
  document.getElementById('node-filter').value='all';document.getElementById('node-sort').value='name';
  const storage = {getItem(){return null;},setItem(){},removeItem(){}};
  class Clock extends Date {constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}}
  const context = {
    document, navigator:{platform:'Linux'}, sessionStorage:storage, localStorage:storage,
    Date:Clock, URL, URLSearchParams, AbortController, console,
    Intl:{DisplayNames:class {constructor(){regionConstructions++;}of(code){return code==='CN'?'中国':code;}}},
    setTimeout:(callback,ms)=>{const id=++nextTimer;timers.set(id,{callback,ms});return id;},clearTimeout:id=>timers.delete(id),setInterval:(callback,ms)=>intervals.push({callback,ms}),
    addEventListener(){},location:{origin:'https://hub.test'},
    fetch:async (url,options)=>{requests.push(url);const result=await fetcher(url,options);return {ok:true,json:async()=>result};},
  };
  context.window=context;
  vm.createContext(context);
  const exposed = source.replace(/\}\)\(\);\s*$/, `globalThis.dashboard={render,renderNodes,refresh,selectPage,refreshLatency,loadHistory,nodeAlerts,currentLatency,offlineReason,latencyBlock,renderTransfers,renderUploadCommand,ring,networkPanel,graph,historyPanel,loadOneDriveSettings,startOneDriveLogin,pollOneDriveLogin,cancelOneDriveLogin,safeVerificationUrl,renderOneDriveSettings,
    setState(value){state=value;},setShell(value){uploadShell=value;},setToken(value){token=value;},setLatency(value){latency=value;},setPrefs(value){prefs={...prefs,...value};},
    expand(id,selection){expanded.set(id,selection);},getHistory(){return history;}};})();`);
  vm.runInContext(exposed, context, {filename:'dashboard.js'});
  return {
    ui:context.dashboard, document, elements, requests, intervals,
    el:id=>document.getElementById(id),timers,
    fireTimer(ms){const entry=[...timers].find(([,timer])=>timer.ms===ms);assert(entry,'expected pending timer '+ms);timers.delete(entry[0]);entry[1].callback();},
    visibility(hidden){document.hidden=hidden;for(const callback of document.listeners.visibilitychange||[])callback();},
    fetch(fn){fetcher=fn;}, advance(ms){now+=ms;},now:()=>now,
    createdBars:()=>all.filter(node=>node.className.startsWith('latency-bar ')).length,
    regionConstructions:()=>regionConstructions,
  };
}
const snapshot = nodes=>({nodes,transfers:[],mcp_url:'https://hub.test/mcp',version:'test',update:{}});
function node(h,id='training') {return {id,name:id,online:true,connection_status:'online',frp_status:'online',last_seen:h.now()/1000,metrics_status:'ok',freshness:{status:'fresh',sampled_at:h.now()/1000},heartbeat_freshness:{status:'fresh'},cpu_percent:25,memory_percent:30,disk_percent:20,gpu_status:'no_gpu',country_code:'CN',latency_ms:22,latency_sample_at:h.now()/1000,latency_success:true};}
const deferred = ()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const flush = ()=>new Promise(resolve=>setImmediate(resolve));

test('poll renders only the current page, and switching uses the latest snapshot',()=>{
  const h=harness();h.ui.setState(snapshot([node(h)]));h.ui.render();
  assert.equal(h.el('nodes').replacements,1);assert.equal(h.el('management-rows').replacements,0);assert.equal(h.el('alerts-list').replacements,0);
  h.ui.selectPage('manage');const nodeRenders=h.el('nodes').replacements;assert.match(h.el('management-rows').textContent,/training/);
  h.ui.setState(snapshot([{...node(h),name:'renamed'}]));h.ui.render();assert.equal(h.el('nodes').replacements,nodeRenders);assert.match(h.el('management-rows').textContent,/renamed/);
  const transfers=h.el('transfers').replacements;h.ui.selectPage('settings');h.ui.render();assert.equal(h.el('transfers').replacements,transfers);assert.equal(h.el('nodes').replacements,nodeRenders);
  h.ui.selectPage('overview');assert.match(h.el('nodes').textContent,/renamed/);assert.equal(h.regionConstructions(),1);
});

test('unchanged latency bars are reused across state polls, but thresholds invalidate them',()=>{
  const h=harness();h.ui.setState(snapshot([node(h)]));h.ui.render();const initial=h.createdBars();assert.equal(initial,96);
  h.ui.setState(snapshot([{...node(h),cpu_percent:77}]));h.ui.render();assert.equal(h.createdBars(),initial);assert.match(h.el('nodes').textContent,/77/);
  h.ui.setPrefs({latencyHealthy:200});h.ui.render();assert.equal(h.createdBars(),192);
  h.ui.setState(snapshot([]));h.ui.render();h.ui.setState(snapshot([node(h)]));h.ui.render();assert.equal(h.createdBars(),288,'revoked nodes must not keep cached DOM indefinitely');
});

test('state is usable before a delayed summary and hidden completion does not repaint overview',async()=>{
  const h=harness(),pending=deferred();h.ui.setToken('synthetic');h.fetch(url=>url==='/api/state'?snapshot([node(h)]):pending.promise);
  assert.equal(await h.ui.refresh(),true);assert.match(h.el('nodes').textContent,/training/);assert.equal(h.requests.length,2);
  h.ui.selectPage('manage');const renders=h.el('nodes').replacements;pending.resolve({generated_at:h.now()/1000,nodes:{}});await flush();
  assert.equal(h.el('nodes').replacements,renders);h.ui.selectPage('overview');assert.equal(h.requests.length,2,'recent summary is retained when returning to overview');
});

test('summary failure is rate limited and hidden pages do not query latency or history',async()=>{
  const h=harness();h.ui.setToken('synthetic');h.ui.setState(snapshot([node(h)]));h.fetch(async()=>{throw new Error('synthetic outage');});
  await h.ui.refreshLatency();assert.equal(h.requests.length,1);h.advance(5000);await h.ui.refreshLatency();assert.equal(h.requests.length,1);
  h.advance(40000);await h.ui.refreshLatency();assert.equal(h.requests.length,2);
  h.ui.selectPage('manage');h.advance(45000);await h.ui.refreshLatency(true);h.ui.expand('training',{metric:'cpu',range:'1h',device:''});await h.ui.loadHistory(node(h),true);assert.equal(h.requests.length,2);
  h.document.hidden=true;h.ui.selectPage('overview');await h.ui.refreshLatency(true);assert.equal(h.requests.length,2);
});

test('history remains lazy, survives page changes, and same key requests do not overlap',async()=>{
  const h=harness(),pending=deferred();h.ui.setState(snapshot([node(h)]));h.fetch(()=>pending.promise);h.ui.expand('training',{metric:'cpu',range:'1h',device:''});
  const first=h.ui.loadHistory(node(h));await h.ui.loadHistory(node(h));assert.equal(h.requests.length,1);
  h.ui.selectPage('manage');const renders=h.el('nodes').replacements;pending.resolve({node_id:'training',points:[],stats:{},gpus:[]});await first;assert.equal(h.el('nodes').replacements,renders);
  h.ui.selectPage('overview');assert.match(h.el('nodes').textContent,/该时段暂无有效观测/);assert.equal(h.requests.length,1);
});

test('sampling delay is distinct from heartbeat health and does not hide fresh RTT',()=>{
  const h=harness(),sample={...node(h),metrics_status:'collecting',freshness:{status:'unknown'},cpu_percent:null};h.ui.setState(snapshot([sample]));h.ui.render();
  assert.match(h.el('nodes').textContent,/指标采集中/);assert.doesNotMatch(h.el('nodes').textContent,/探针心跳中断/);assert.equal(h.ui.currentLatency(sample),22);
  sample.metrics_status='error';sample.freshness={status:'stale'};sample.metrics_error='sample_failed';h.ui.render();assert.match(h.el('nodes').textContent,/指标采集失败/);
  assert(h.ui.nodeAlerts(sample).some(issue=>issue.text==='指标采集失败'));
  sample.connection_status='unknown';sample.online=false;sample.frp_status=null;assert.equal(h.ui.offlineReason(sample),'SSH 隧道状态未知，探针仍在上报');
});

test('management reports hub monitor failures with nullable and legacy data',()=>{
  const h=harness();const state=snapshot([]);state.hub={uptime_seconds:75,startup_ms:42,monitor:{status:'degraded',last_completed_at:null,error_code:'storage_error'},frp:{status:'stale',request_ms:4,error_code:null}};
  h.ui.setState(state);h.ui.selectPage('manage');assert.equal(h.el('hub-panel').hidden,false);assert.match(h.el('hub-monitor').textContent,/巡检异常/);assert.match(h.el('hub-message').textContent,/SSH 隧道状态暂不可确认/);assert.equal(h.el('hub-checked').textContent,'暂无记录');
  h.ui.setState(snapshot([]));h.ui.render();assert.equal(h.el('hub-panel').hidden,true);
});

test('stale transfer progress never claims failure and completion keeps precedence',()=>{
  const h=harness(),state=snapshot([node(h)]);state.transfers=[{node_id:'training',status:'transferring',phase:'transferring',progress_stale:true,bytes_done:50,bytes_total:100,speed_bps:999},{node_id:'training',status:'completed',phase:'completed',authorization_expired:true,bytes_done:100,bytes_total:100,speed_bps:10,transfer_seconds:10}];h.ui.setState(state);h.ui.render();
  const rows=h.el('transfers').children;assert.match(rows[0].textContent,/进度暂时失联/);assert.match(rows[0].textContent,/文件结果待确认/);assert.doesNotMatch(rows[0].textContent,/失败|\/s/);assert.match(rows[1].textContent,/已完成/);assert.doesNotMatch(rows[1].textContent,/文件结果待确认/);
});


test('GPU freshness and alerts remain independent when base resource collection stalls',()=>{
  const h=harness(),sample={...node(h),metrics_status:'collecting',freshness:{status:'unknown'},cpu_percent:99,gpu_percent:99,gpu_status:'ok'};
  h.ui.setState(snapshot([sample]));h.ui.render();
  let issues=h.ui.nodeAlerts(sample);assert(issues.some(issue=>issue.severity==='critical'&&issue.text.startsWith('GPU 占用')));assert(!issues.some(issue=>issue.text.startsWith('CPU 占用')));
  assert(!h.ui.ring(sample,'gpu').className.split(' ').includes('stale'));assert(h.ui.ring(sample,'cpu').className.split(' ').includes('stale'));
  const card=h.el('nodes').children[0];assert.match(card.children.at(-1).children[1].title,/未知时间/,'a collecting agent has no successful metric timestamp');
  sample.gpu_status='error';issues=h.ui.nodeAlerts(sample);assert(issues.some(issue=>issue.text==='GPU 采集失败'));
  sample.heartbeat_freshness={status:'stale'};issues=h.ui.nodeAlerts(sample);assert(!issues.some(issue=>issue.text.startsWith('GPU ')));assert(h.ui.ring(sample,'gpu').className.split(' ').includes('stale'));
});

test('hub memory displays HTTP process RSS and clears nullable or legacy values',()=>{
  const h=harness(),state=snapshot([]);state.hub={process_rss_bytes:64*1024*1024,monitor:{status:'ok'},frp:{status:'ok'}};
  h.ui.setState(state);h.ui.selectPage('manage');assert.equal(h.el('hub-rss').textContent,'64.0 MiB');
  state.hub.process_rss_bytes=null;h.ui.render();assert.equal(h.el('hub-rss').textContent,'—');
  delete state.hub.process_rss_bytes;h.ui.render();assert.equal(h.el('hub-rss').textContent,'—');
});

test('update preparation distinguishes dependency reuse, no reuse, and older records',()=>{
  const h=harness(),state=snapshot([]);state.update={status:'success',prepare_seconds:12.34,dependencies_reused:true};
  h.ui.setState(state);h.ui.selectPage('manage');assert.equal(h.el('update-prepare').textContent,'12.3 秒');assert.equal(h.el('update-dependencies').textContent,'已复用');
  state.update={status:'success',prepare_seconds:0,dependencies_reused:false};h.ui.render();assert.equal(h.el('update-prepare').textContent,'0.0 秒');assert.equal(h.el('update-dependencies').textContent,'未复用');
  state.update={status:'idle',prepare_seconds:null,dependencies_reused:null};h.ui.render();assert.equal(h.el('update-prepare').textContent,'—');assert.equal(h.el('update-dependencies').textContent,'未知');
  state.update={status:'idle'};h.ui.render();assert.equal(h.el('update-prepare').textContent,'—');assert.equal(h.el('update-dependencies').textContent,'未知');
});

test('agent update failures and rollback are visible without replacing node connection status',()=>{
  const h=harness(),sample={...node(h),agent_version:'current12345',agent_update:{status:'rolled_back',version:'candidate678',checked_at:h.now()/1000-30,updated_at:h.now()/1000,error:'startup_failed'}};
  h.ui.setState(snapshot([sample]));h.ui.selectPage('manage');
  let row=h.el('management-rows').children[0];assert.match(row.children[0].textContent,/当前探针 current12345/);assert.match(row.children[0].textContent,/更新已回滚.*更新版本 candidate678/);assert.equal(row.children[1].textContent,'在线');
  let update=row.children[0].children[2].children[1];assert(update.className.includes('bad'));assert.match(update.title,/startup_failed/);
  sample.agent_update={status:'check_failed',version:null,checked_at:null,updated_at:null,error:'request_failed'};sample.online=false;sample.connection_status='offline';sample.frp_status='offline';h.ui.render();
  row=h.el('management-rows').children[0];assert.match(row.children[0].textContent,/更新检查失败/);assert.equal(row.children[1].textContent,'离线');
  for(const [status,label]of [['idle','无待处理更新'],['deferred','等待空闲更新'],['pending','更新待确认'],['confirmed','更新已确认']]){sample.agent_update={status};h.ui.render();const name=h.el('management-rows').children[0].children[0];assert.match(name.textContent,new RegExp(label));assert(!name.children[2].children[1].className.includes('bad'));}
  delete sample.agent_update;delete sample.agent_version;h.ui.render();row=h.el('management-rows').children[0];assert.match(row.children[0].textContent,/当前探针 版本未知/);assert.match(row.children[0].textContent,/更新状态未知/);assert.equal(row.children[1].textContent,'离线');
});

test('OneDrive must be connected before selection and legacy state keeps the P2P command',()=>{
  const h=harness(),state=snapshot([node(h)]);h.ui.setState(state);h.ui.render();
  h.el('upload-source').value='/local/data';h.el('upload-destination').value='/data/training';h.ui.renderUploadCommand();
  assert.equal(h.el('upload-transport').value,'p2p');assert.equal(h.el('upload-onedrive').disabled,true);assert.match(h.el('upload-onedrive').textContent,/需管理员连接/);assert.equal(h.el('onedrive-availability').hidden,false);
  assert.doesNotMatch(h.el('transfer-command').textContent,/--transport|-Transport/);assert.equal(h.el('copy-transfer').disabled,false);
  state.storage={onedrive:{enabled:true}};h.ui.render();assert.equal(h.el('upload-onedrive').disabled,false);assert.equal(h.el('onedrive-availability').hidden,true);assert.doesNotMatch(h.el('transfer-command').textContent,/--transport|-Transport/);
  h.el('upload-transport').value='onedrive';state.storage.onedrive.enabled=false;h.ui.renderUploadCommand();assert.equal(h.el('upload-transport').value,'p2p');assert.doesNotMatch(h.el('transfer-command').textContent,/--transport|-Transport/);
});

test('selected OneDrive appends the correct Bash or PowerShell option without changing path quoting',()=>{
  const h=harness(),state=snapshot([node(h)]);state.storage={onedrive:{enabled:true}};h.ui.setState(state);h.ui.render();
  h.el('upload-source').value="/local/dataset's files";h.el('upload-destination').value='/data/training files';h.el('upload-overwrite').checked=true;h.el('upload-transport').value='onedrive';h.ui.renderUploadCommand();
  let command=h.el('transfer-command').textContent;assert.match(command,/--transport onedrive$/);assert(command.includes("--source '/local/dataset'\"'\"'s files'"));assert(command.includes("--destination '/data/training files' --overwrite"));assert.match(h.el('upload-transport-note').textContent,/训练机自动下载/);
  h.ui.setShell('powershell');h.el('upload-source').value="D:\\dataset's files";h.ui.renderUploadCommand();command=h.el('transfer-command').textContent;assert.match(command,/-Transport onedrive$/);assert(command.includes("-Source 'D:\\dataset''s files'"));assert(command.includes("-Destination '/data/training files' -Overwrite"));
  h.el('upload-transport').value='p2p';h.ui.renderUploadCommand();assert.doesNotMatch(h.el('transfer-command').textContent,/-Transport/);assert.match(h.el('upload-transport-note').textContent,/P2P 直连失败即停止/);
});

test('OneDrive upload and training download progress stay separate until node confirmation',()=>{
  const h=harness(),state=snapshot([node(h)]);const common={node_id:'training',mode:'onedrive',bytes_total:100,uploaded_bytes:100,bytes_done:0,downloaded_bytes:0};
  state.transfers=[{...common,status:'uploading',phase:'uploading'},{...common,status:'ready',phase:'ready'},{...common,status:'downloading',phase:'downloading',bytes_done:40,downloaded_bytes:40}];h.ui.setState(state);h.ui.render();
  const rows=h.el('transfers').children;
  for(const row of rows){assert.match(row.textContent,/OneDrive 中转/);assert.doesNotMatch(row.textContent,/中枢中继|已完成/);assert.match(row.textContent,/本机 → OneDrive/);assert.match(row.textContent,/OneDrive → 训练机/);}
  assert.match(rows[0].textContent,/上传至 OneDrive/);assert.match(rows[1].textContent,/等待训练机/);assert.match(rows[2].textContent,/训练机下载中/);
  const progress=row=>row.children.filter(child=>child.attributes.role==='progressbar').map(child=>child.attributes['aria-valuenow']);
  assert.deepEqual(progress(rows[0]),['100.0','0.0']);assert.deepEqual(progress(rows[1]),['100.0','0.0']);assert.deepEqual(progress(rows[2]),['100.0','40.0']);
});

test('OneDrive terminal states and pending cleanup take precedence over stale progress',()=>{
  const h=harness(),state=snapshot([node(h)]),common={mode:'onedrive',node_id:'training',bytes_total:100,uploaded_bytes:100,downloaded_bytes:100,progress_stale:true};
  state.transfers=[{...common,status:'completed',cleanup_pending:true},{...common,status:'expired',cleanup_pending:true},{...common,status:'failed',error:'download_failed'},{...common,status:'cancelled'},{...common,status:'downloading',downloaded_bytes:10}];h.ui.setState(state);h.ui.render();
  const rows=h.el('transfers').children;
  assert.match(rows[0].textContent,/已完成/);assert.match(rows[0].textContent,/网盘待清理/);assert.doesNotMatch(rows[0].textContent,/进度暂时失联|文件结果待确认/);
  assert.match(rows[1].textContent,/已过期/);assert.match(rows[1].textContent,/网盘待清理/);assert.doesNotMatch(rows[1].textContent,/已完成/);
  assert.match(rows[2].textContent,/失败.*download_failed/);assert.match(rows[3].textContent,/已取消/);assert.match(rows[4].textContent,/进度暂时失联.*文件结果待确认/);assert.doesNotMatch(rows[4].textContent,/已完成/);
});

function networkSample(h){return {network_rx_bps:2048,network_tx_bps:4096,network_rx:1048576,network_tx:2097152,network_interfaces:['eth0'],network_scope:'default_route',network_status:'ok',network_sampled_at:h.now()/1000,network_interval_seconds:10,network_counter_scope:'interface_lifetime',network_rx_errors:0,network_tx_errors:1,network_rx_dropped:2,network_tx_dropped:0};}

test('network card separates rates, interface lifetime totals, and unknown observations',()=>{
  const h=harness(),sample={...node(h),...networkSample(h),metrics_status:'collecting',freshness:{status:'unknown'}};
  let panel=h.ui.networkPanel(sample);assert.match(panel.textContent,/2\.0 KiB\/s/);assert.match(panel.textContent,/4\.0 KiB\/s/);assert.match(panel.textContent,/累计接收 1\.0 MiB · 发送 2\.0 MiB/);assert.match(panel.textContent,/默认路由接口 · eth0/);assert.match(panel.textContent,/主机重启或接口变化后累计计数可能重置/);assert.match(panel.textContent,/不代表端到端丢包率/);
  sample.network_status='reset';sample.network_rx_bps=null;sample.network_tx_bps=null;panel=h.ui.networkPanel(sample);assert.match(panel.textContent,/等待新的速率采样/);assert.equal(panel.children[0].children[0].children[1].textContent,'—');
  sample.network_status='ok';sample.network_rx_bps=2048;h.advance(36000);panel=h.ui.networkPanel(sample);assert.match(panel.textContent,/网络观测已过期/);assert.doesNotMatch(panel.children[0].textContent,/KiB\/s|0 B\/s/);
});

test('network history uses byte rates, keeps gaps, and preserves percentage resource charts',async()=>{
  const h=harness(),sample={...node(h),...networkSample(h)},data={node_id:'training',metric:'network_rx',unit:'bytes_per_second',points:[{timestamp:h.now()/1000-120,value:20480},{timestamp:h.now()/1000-60,value:null},{timestamp:h.now()/1000,value:81920}],stats:{min:20480,max:81920,avg:51200}};
  h.ui.setState(snapshot([sample]));h.ui.render();h.fetch(url=>{assert.match(url,/history\?metric=network_rx&range=1h/);return data;});
  h.ui.networkPanel(sample).children[0].children[0].listeners.click[0]();await flush();
  const panel=h.el('nodes').children[0].children.at(-1);assert.match(panel.textContent,/网络下行 速率/);assert.match(panel.textContent,/最小 20\.0 KiB\/s/);assert.doesNotMatch(panel.textContent,/%/);
  const drawing=h.ui.graph(data,{metric:'network_rx',range:'1h'},'blue'),chart=drawing.children[0],trace=chart.children.find(item=>item.tagName==='PATH');assert.equal((trace.attributes.d.match(/M /g)||[]).length,2);assert(!trace.attributes.d.includes(' L '));chart.listeners.focus[0]();assert.match(drawing.children[1].textContent,/80\.0 KiB\/s/);
  const empty=h.ui.graph({...data,points:[{timestamp:h.now()/1000,value:null}]},{metric:'network_rx',range:'1h'},'blue');assert.match(empty.textContent,/暂无有效观测/);assert.doesNotMatch(empty.textContent,/0 B\/s/);
  const cpu=h.ui.graph({...data,points:[{timestamp:h.now()/1000,value:25}]},{metric:'cpu',range:'1h'},'green');cpu.children[0].listeners.focus[0]();assert.match(cpu.children[1].textContent,/25\.0%/);
});

test('hub network shows host traffic without offering unsupported history controls',()=>{
  const h=harness(),state=snapshot([]);state.hub={monitor:{status:'ok'},frp:{status:'ok'},network:networkSample(h)};h.ui.setState(state);h.ui.selectPage('manage');
  const panel=h.el('hub-network').children[0];assert.equal(panel.attributes['aria-label'],'中枢网络与流量');assert.match(panel.textContent,/2\.0 KiB\/s/);assert(panel.children[0].children.every(item=>item.tagName==='DIV'&&!item.listeners.click));
});

const testClientId='00000000-1111-2222-3333-444444444444';
const settingsStatus=(extra={})=>({configured:false,connected:false,auth_state:'unconfigured',client_id:testClientId,quota:null,error_code:null,login:null,can_switch_account:true,...extra});
const loginSession=(h,extra={})=>({session_id:'public-session',status:'pending',user_code:'ABCD-EFGH',verification_uri:'https://microsoft.com/devicelogin',expires_at:h.now()/1000+900,interval:5,error_code:null,...extra});

test('device login displays only official link and public code, pauses off page, and confirms connection',async()=>{
  const h=harness(),state=snapshot([node(h)]);let started=false,connected=false;h.ui.setState(state);h.ui.setToken('synthetic');
  h.fetch((url,options)=>{
    if(url==='/api/storage/onedrive')return settingsStatus(connected?{configured:true,connected:true,auth_state:'connected',quota:{total:1073741824,remaining:536870912}}:{login:started?loginSession(h):null});
    if(url==='/api/storage/onedrive/login'){assert.equal(options.method,'POST');assert.deepEqual(JSON.parse(options.body),{client_id:testClientId});started=true;return loginSession(h);}
    if(url==='/api/storage/onedrive/login/public-session'){connected=true;return loginSession(h,{status:'connected',user_code:null,verification_uri:null});}
    if(url==='/api/state')return {...state,storage:{onedrive:{enabled:connected}}};
    throw new Error('unexpected route');
  });
  h.ui.selectPage('settings');await flush();assert.equal(h.el('onedrive-client-id').value,testClientId);assert.equal(h.el('onedrive-connection').textContent,'尚未连接');await h.ui.startOneDriveLogin();
  assert.equal(h.el('onedrive-user-code').textContent,'ABCD-EFGH');assert.equal(h.el('onedrive-login-link').attributes.href,'https://microsoft.com/devicelogin');assert.equal(h.el('onedrive-connection').textContent,'等待网页登录');assert([...h.timers.values()].some(timer=>timer.ms===5000));
  h.ui.selectPage('manage');assert(![...h.timers.values()].some(timer=>timer.ms===5000));h.ui.selectPage('settings');await flush();assert([...h.timers.values()].some(timer=>timer.ms===5000));
  h.visibility(true);assert(![...h.timers.values()].some(timer=>timer.ms===5000));h.visibility(false);await flush();h.fireTimer(5000);await flush();
  assert.equal(h.el('onedrive-connection').textContent,'已连接');assert.equal(h.el('onedrive-login-panel').hidden,true);assert.equal(h.el('onedrive-user-code').textContent,'');assert.match(h.el('onedrive-quota').textContent,/可用 512\.0 MiB/);assert(![...h.timers.values()].some(timer=>timer.ms===5000));
});

test('cancelling login ignores a late connected response and does not restart polling',async()=>{
  const h=harness(),pending=deferred();let cancelled=false;h.ui.setState(snapshot([]));h.ui.setToken('synthetic');h.fetch((url,options)=>{
    if(url==='/api/storage/onedrive')return settingsStatus();
    if(url==='/api/storage/onedrive/login')return loginSession(h);
    if(url.endsWith('/cancel')){assert.equal(options.method,'POST');cancelled=true;return loginSession(h,{status:'cancelled',user_code:null,verification_uri:null});}
    if(url.endsWith('/public-session'))return pending.promise;
    throw new Error('unexpected route');
  });
  h.ui.selectPage('settings');await flush();await h.ui.startOneDriveLogin();const polling=h.ui.pollOneDriveLogin();await h.ui.cancelOneDriveLogin();assert(cancelled);pending.resolve(loginSession(h,{status:'connected',user_code:null,verification_uri:null}));await polling;
  assert.equal(h.el('onedrive-connection').textContent,'尚未连接');assert.equal(h.el('onedrive-user-code').textContent,'');assert(![...h.timers.values()].some(timer=>timer.ms===5000));
});

test('untrusted login URLs and unknown provider errors are never echoed as actionable content',async()=>{
  const h=harness();h.ui.setState(snapshot([]));h.ui.setToken('synthetic');h.fetch(()=>settingsStatus({login:loginSession(h,{verification_uri:'https://microsoft.com.evil.test/devicelogin'})}));h.ui.selectPage('settings');await flush();
  assert.equal(h.el('onedrive-login-link').hidden,true);assert.equal(h.el('onedrive-login-link').attributes.href,undefined);assert.match(h.el('onedrive-error').textContent,/无法验证登录信息/);
  for(const url of ['javascript:alert(1)','https://microsoft.com@evil.test/devicelogin','https://microsoft.com/devicelogin?redirect=https://evil.test','http://microsoft.com/devicelogin'])assert.equal(h.ui.safeVerificationUrl(url),null);
  h.fetch(()=>{throw new Error('raw-provider-secret-detail');});await h.ui.loadOneDriveSettings();assert.equal(h.el('onedrive-error').textContent,'登录未完成，请重试。');assert.doesNotMatch(h.el('onedrive-error').textContent,/raw-provider/);assert.equal(h.el('onedrive-connection').textContent,'连接状态暂不可确认');
});

test('OneDrive settings explain Fleet-only permanent cleanup and allow same-account reauthorization',async()=>{
  const h=harness(),state=snapshot([]);state.storage={onedrive:{cleanup:{mode:'permanent',scope:'fleet_temp_only',pending_count:2,error_count:1,last_attempt_at:h.now()/1000,last_success_at:null,error_code:'provider_unavailable',legacy_recycle_bin:'manual'}}};h.ui.setState(state);h.ui.setToken('synthetic');
  h.fetch(()=>settingsStatus({configured:true,auth_state:'authorization_required',can_switch_account:false}));h.ui.selectPage('settings');await flush();
  assert.equal(h.el('onedrive-login-start').disabled,false);assert.match(h.el('onedrive-account-note').textContent,/仅可重新授权当前账户/);assert.match(h.el('onedrive-cleanup-policy').textContent,/永久清除 Fleet 暂存文件，不进入回收站/);assert.match(h.el('onedrive-cleanup-status').textContent,/待清理 2 项.*1 项清理失败，将自动重试/);
  delete state.storage;h.ui.setToken('');h.ui.setState(state);h.ui.render();assert.equal(h.el('onedrive-cleanup-status').textContent,'清理状态尚无观测');
});

"""Browser-native generation archive, endpoint, and in-duel preview controls."""

import json


GENERATION_CONTROL_PANEL_HTML = r"""
<details id="generation-control-panel">
  <summary>
    <span>Generation engine &amp; local diffuser</span>
    <span class="gc-summary-detail" id="gcSummaryDetail">Checking active archive...</span>
  </summary>
  <div class="gc-body">
    <div class="gc-mode-row" aria-label="Generation archive">
      <button type="button" data-gc-mode="novelai">NovelAI archive</button>
      <button type="button" data-gc-mode="local">Local / Anima archive</button>
    </div>
    <p class="gc-note">Each engine keeps its own ladder, buffer, gallery, prompts, and history. Switching finishes any image already in flight, saves the current archive, and reloads the selected one.</p>
    <section class="gc-local" id="gcLocal" hidden>
      <div class="gc-form">
        <div class="gc-field">
          <label for="gcBackend">Diffuser type</label>
          <select id="gcBackend"><option value="comfyui">ComfyUI</option><option value="forge">Forge / Neo Forge</option></select>
        </div>
        <div class="gc-field">
          <label for="gcUrl">Manual endpoint</label>
          <input id="gcUrl" type="url" placeholder="http://127.0.0.1:8188">
        </div>
        <label class="gc-check"><input id="gcAllowLan" type="checkbox">Allow an endpoint elsewhere on my LAN</label>
      </div>
      <div class="gc-actions">
        <button id="gcTest" type="button">Test connection</button>
        <button id="gcSave" type="button">Save and connect</button>
        <button id="gcScan" type="button">Scan running endpoints</button>
      </div>
      <div class="gc-endpoints" id="gcEndpoints"></div>
      <div class="gc-preview-settings">
        <div><strong>In-duel live preview</strong><span>Show the first buffered duels as they form inside the normal A/B cards.</span></div>
        <label for="gcPreviewCount">First duels (0-10)</label>
        <input id="gcPreviewCount" type="number" min="0" max="10" step="1">
        <button id="gcPreviewSave" type="button">Save</button>
      </div>
      <div class="gc-preview-maintenance-status" id="gcPreviewStatus"></div>
    </section>
    <div class="gc-message" id="gcMessage" role="status"></div>
  </div>
</details>
""".strip()


GENERATION_CONTROL_HEAD = r"""
<style id="artist-ranker-generation-control-style">
  #generation-control-server-host{width:100%}
  #generation-control-panel{margin:10px 0 14px;width:100%;border:1px solid color-mix(in srgb,var(--primary-500,#70a8ff) 32%,rgba(255,255,255,.1));border-radius:15px;background:color-mix(in srgb,var(--block-background-fill,#151a22) 94%,var(--primary-500,#70a8ff) 6%);color:var(--body-text-color,#eef3fb);overflow:hidden;box-shadow:0 12px 30px rgba(0,0,0,.12)}
  #generation-control-panel>summary{cursor:pointer;list-style:none;display:flex;align-items:center;justify-content:space-between;gap:10px;padding:12px 14px;font-weight:780}
  #generation-control-panel>summary::-webkit-details-marker{display:none}
  .gc-summary-detail{color:var(--body-text-color-subdued,#9aa8bc);font-size:.78rem;font-weight:600}
  .gc-body{padding:0 14px 14px}.gc-mode-row,.gc-actions,.gc-endpoints{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
  .gc-mode-row button,.gc-actions button,.gc-endpoints button,.gc-preview-settings button{min-height:39px;border:1px solid rgba(255,255,255,.13);border-radius:11px;background:var(--block-background-fill,#222b3a);color:inherit;padding:8px 12px;font-weight:730;cursor:pointer}
  .gc-mode-row button.active{border-color:#70a8ff;box-shadow:inset 0 0 0 1px #70a8ff;background:rgba(112,168,255,.14)}
  .gc-note,.gc-message,.gc-preview-maintenance-status{color:var(--body-text-color-subdued,#9aa8bc);font-size:.78rem;line-height:1.42}.gc-message{min-height:1.2em;margin-top:8px}.gc-message.error{color:#ff9494}.gc-message.success{color:#73e99f}
  .gc-local{margin-top:11px;padding-top:10px;border-top:1px solid rgba(255,255,255,.08)}.gc-form{display:grid;grid-template-columns:180px minmax(240px,1fr) auto;gap:8px;align-items:end}.gc-field{display:grid;gap:5px}.gc-field label{font-size:.7rem;color:var(--body-text-color-subdued,#9aa8bc)}
  .gc-field input,.gc-field select{width:100%;min-height:40px;border:1px solid rgba(255,255,255,.13);border-radius:10px;background:#0e131a!important;color:#eef3fb!important;padding:8px 10px}.gc-field input::placeholder{color:#778499}.gc-check{display:flex;align-items:center;gap:7px;min-height:40px;font-size:.78rem}.gc-check input{width:20px;height:20px}
  .gc-actions,.gc-endpoints{margin-top:8px}.gc-endpoints button{font-size:.75rem;text-align:left}.gc-endpoints button span{display:block;color:var(--body-text-color-subdued,#9aa8bc);font-size:.68rem;font-weight:500}
  .gc-preview-settings{display:grid;grid-template-columns:minmax(220px,1fr) auto 76px auto;align-items:center;gap:9px;margin-top:12px;padding-top:11px;border-top:1px solid rgba(255,255,255,.08)}.gc-preview-settings>div{display:grid;gap:2px}.gc-preview-settings>div span{font-size:.72rem;color:var(--body-text-color-subdued,#9aa8bc)}.gc-preview-settings label{font-size:.74rem}.gc-preview-settings input{width:76px;min-height:38px;border-radius:9px;border:1px solid rgba(255,255,255,.13);background:var(--input-background-fill,#0e131a);color:inherit;padding:7px}.gc-preview-maintenance-status{margin-top:8px}

  #gc-duel-generation-bar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:10px 0;padding:10px 12px;border:1px solid rgba(112,168,255,.34);border-radius:14px;background:linear-gradient(135deg,rgba(61,117,201,.18),rgba(20,28,39,.86));box-shadow:0 10px 26px rgba(0,0,0,.14)}
  #gc-duel-generation-bar[hidden]{display:none!important}.gc-stage-bar-copy{display:flex;align-items:center;gap:10px;min-width:0}.gc-live-dot{width:9px;height:9px;flex:0 0 auto;border-radius:50%;background:#76e4ff;box-shadow:0 0 0 5px rgba(118,228,255,.1);animation:gcPulse 1.5s ease-in-out infinite}.gc-stage-bar-text{display:grid;min-width:0}.gc-stage-bar-text strong{font-size:.84rem}.gc-stage-bar-text span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--body-text-color-subdued,#aab6c8);font-size:.7rem}.gc-stage-bar-actions{display:flex;gap:7px;flex:0 0 auto}.gc-stage-bar-actions button{min-height:35px;border:1px solid rgba(255,255,255,.14);border-radius:10px;background:rgba(10,17,25,.58);color:inherit;padding:7px 10px;font-weight:720;cursor:pointer}
  .gc-preview-target{position:relative!important;isolation:isolate}.gc-preview-target.gc-preview-visible{min-height:clamp(260px,42vw,700px);overflow:hidden!important}.gc-preview-target.gc-preview-visible>:not(.gc-stage-preview){visibility:hidden!important}.gc-stage-preview{display:none;position:absolute;inset:0;place-items:center;min-height:0;border-radius:12px;overflow:hidden;background:radial-gradient(circle at 50% 35%,rgba(80,124,194,.20),transparent 48%),#090d13}.gc-preview-visible>.gc-stage-preview{display:grid}.gc-stage-preview:before{content:"";position:absolute;inset:-40%;background:linear-gradient(105deg,transparent 42%,rgba(255,255,255,.08) 50%,transparent 58%);animation:gcShimmer 2.2s linear infinite}.gc-stage-preview img{position:relative;z-index:1;display:block;width:100%;height:100%;min-height:0;max-height:none;object-fit:contain}.gc-stage-preview:not(.has-image) img{visibility:hidden}.gc-stage-preview-badge{position:absolute;z-index:2;left:10px;bottom:10px;border:1px solid rgba(255,255,255,.12);border-radius:999px;background:rgba(5,9,14,.82);backdrop-filter:blur(8px);padding:5px 9px;color:#eef6ff;font-size:.7rem;font-weight:750}.gc-stage-preview-side{position:absolute;z-index:2;right:10px;top:10px;width:29px;height:29px;display:grid;place-items:center;border-radius:9px;background:rgba(5,9,14,.78);font-weight:850}
  html.gc-watching-generation #compare-duel-images,html.gc-watching-generation #gestureZone{pointer-events:none!important;user-select:none!important}html.gc-watching-generation #pick-a-btn,html.gc-watching-generation #pick-b-btn,html.gc-watching-generation #tie-btn,html.gc-watching-generation #both-bad-btn,html.gc-watching-generation #invalid-btn,html.gc-watching-generation #skip-btn{filter:saturate(.3);opacity:.56;pointer-events:none!important}
  @keyframes gcPulse{50%{opacity:.48;transform:scale(.82)}}@keyframes gcShimmer{to{transform:translateX(45%)}}
  @media(max-width:680px){#generation-control-panel{margin:7px 0 9px}.gc-form{grid-template-columns:1fr}.gc-summary-detail{display:none}.gc-mode-row button{flex:1 1 120px}.gc-preview-settings{grid-template-columns:1fr auto auto}.gc-preview-settings>div{grid-column:1/-1}#gc-duel-generation-bar{align-items:flex-start;flex-direction:column}.gc-stage-bar-actions{width:100%}.gc-stage-bar-actions button{flex:1}.gc-preview-target.gc-preview-visible{min-height:52svh}}
</style>
<script id="artist-ranker-generation-control-client-v2">
(() => {
  if (window.__artistRankerGenerationControlV2) return;
  window.__artistRankerGenerationControlV2 = true;

  const state = {payload:null, previews:null, previewTimer:null, restarting:false};
  const panelHtml = () => __GENERATION_CONTROL_PANEL_HTML__;
  const byId = id => document.getElementById(id);
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const message = (text='', kind='') => {const node=byId('gcMessage');if(node){node.textContent=text;node.className='gc-message'+(kind?` ${kind}`:'')}};
  async function api(url, options={}) {const response=await fetch(url,{cache:'no-store',...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});let body={};try{body=await response.json()}catch{}if(!response.ok)throw new Error(body.detail||`HTTP ${response.status}`);return body}

  function installPanel() {
    let panel = byId('generation-control-panel');
    if (!panel) {
      const generationPage = byId('generationPage');
      if (!generationPage) return false;
      const wrapper = document.createElement('div');
      wrapper.innerHTML = panelHtml();
      panel = wrapper.firstElementChild;
      const card = byId('generationCard');
      if (card) card.before(panel); else generationPage.append(panel);
    }
    if (panel.dataset.gcWired === '1') return true;
    panel.dataset.gcWired = '1';
    wirePanel();
    if (state.payload) render(state.payload);
    return true;
  }

  function installDuelStage() {
    const desktopHost=byId('compare-duel-panel'), desktopAnchor=byId('compare-duel-images');
    const dedicatedHost=byId('duelShell'), dedicatedAnchor=byId('gestureZone');
    const host=desktopHost||dedicatedHost, anchor=desktopAnchor||dedicatedAnchor;
    if (!host || !anchor) return false;
    let bar=byId('gc-duel-generation-bar');
    if (!bar) {
      bar=document.createElement('div');
      bar.id='gc-duel-generation-bar';
      bar.hidden=true;
      bar.innerHTML='<div class="gc-stage-bar-copy"><span class="gc-live-dot"></span><div class="gc-stage-bar-text"><strong id="gcStageTitle">Generating buffered duel</strong><span id="gcStageDetail">Preparing live frames...</span></div></div><div class="gc-stage-bar-actions"><button type="button" data-gc-open-settings>Generation settings</button></div>';
      host.insertBefore(bar,anchor);
    }
    [['compare-image-a','A'],['compare-image-b','B'],['stageA','A'],['stageB','B']].forEach(([id,side])=>{const target=byId(id);if(target){target.classList.add('gc-preview-target');ensureOverlay(target,side)}});
    return true;
  }

  function ensureOverlay(target, side) {
    let overlay=target.querySelector(`.gc-stage-preview[data-side="${side}"]`);
    if (overlay) return overlay;
    overlay=document.createElement('div');
    overlay.className='gc-stage-preview';
    overlay.dataset.side=side;
    overlay.setAttribute('aria-live','polite');
    overlay.innerHTML=`<img alt="Live generation preview for image ${side}"><span class="gc-stage-preview-side">${side}</span><span class="gc-stage-preview-badge">Waiting for first frame</span>`;
    target.appendChild(overlay);
    return overlay;
  }

  function currentImageReady(side) {
    const dedicated=byId(`img${side}`);
    const desktopRoot=byId(`compare-image-${side.toLowerCase()}`);
    const desktop=[...(desktopRoot?.querySelectorAll('img')||[])].find(image=>!image.closest('.gc-stage-preview'));
    const image=dedicated||desktop;
    const source=String(image?.currentSrc||image?.getAttribute?.('src')||'');
    return Boolean(source && !source.startsWith('data:image/svg'));
  }

  function statusText(value={}) {
    if (value.complete) return value.success===false?'failed':'ready';
    const percent=Math.round(Number(value.progress||0)*100);
    return percent ? `${percent}%` : 'waiting';
  }

  function paintSide(side, value, visible) {
    const target=byId(side==='A'?(byId('stageA')?'stageA':'compare-image-a'):(byId('stageB')?'stageB':'compare-image-b'));
    if (!target) return;
    const overlay=ensureOverlay(target,side), image=overlay.querySelector('img'), badge=overlay.querySelector('.gc-stage-preview-badge');
    target.classList.toggle('gc-preview-visible',Boolean(visible));
    if (!visible) return;
    const url=String(value?.image_url||'');
    if (url && image.getAttribute('src')!==url) image.setAttribute('src',url);
    overlay.classList.toggle('has-image',Boolean(url));
    badge.textContent=url?`${value?.complete?'Final frame':statusText(value)} · local generation`:'Waiting for first frame';
  }

  function renderPreviews(previews) {
    state.previews=previews||{};
    installDuelStage();
    const items=Array.isArray(previews?.items)?previews.items:[];
    const item=items[0]||null;
    const maintenance=byId('gcPreviewStatus');
    if (!item) {
      byId('gc-duel-generation-bar')?.setAttribute('hidden','');
      paintSide('A',{},false);paintSide('B',{},false);
      document.documentElement.classList.remove('gc-watching-generation');
      if (maintenance) maintenance.textContent=previews?.supported?(previews?.enabled?'Live preview is armed. It appears in Compare when the buffer generates a new duel.':'Live previews are off. Set a value from 1 to 10 to enable them.'):(previews?.novelai_note||'This engine does not expose live API frames.');
      return;
    }
    const bar=byId('gc-duel-generation-bar');
    if (!bar) return;
    bar.hidden=false;
    const a=item.sides?.A||{}, b=item.sides?.B||{};
    const complete=Boolean(a.complete&&b.complete), failed=Boolean((a.complete&&a.success===false)||(b.complete&&b.success===false)), hasCurrent=Boolean(currentImageReady('A')&&currentImageReady('B'));
    const visible=!hasCurrent;
    byId('gcStageTitle').textContent=failed?'Generation stopped':(complete?'Buffered duel ready':(hasCurrent?(Number(item.slot||1)<=1?'Generating the next buffered duel':`Generating buffered duel ${item.slot}`):'Generating the first playable duel'));
    byId('gcStageDetail').textContent=`Image A ${statusText(a)} · Image B ${statusText(b)}${hasCurrent?' · current duel remains ready':''}${item.backend?` · ${item.backend}`:''}`;
    paintSide('A',a,visible);paintSide('B',b,visible);
    document.documentElement.classList.toggle('gc-watching-generation',visible);
    if (maintenance) maintenance.textContent=`Live preview active in Compare: image A ${statusText(a)}, image B ${statusText(b)}.`;
  }

  function openSettings() {
    if (byId('generationPage')) {
      const ribbon=byId('navigationRibbon');
      if (ribbon) ribbon.click();
      else {if(byId('duelPage'))byId('duelPage').hidden=true;byId('generationPage').hidden=false}
    } else {
      const tab=[...document.querySelectorAll('[role="tab"]')].find(node=>String(node.textContent||'').trim()==='Maintenance');
      tab?.click();
    }
    let attempts=0;
    const reveal=()=>{installPanel();const panel=byId('generation-control-panel');if(panel){panel.open=true;panel.scrollIntoView({behavior:'smooth',block:'start'});return}if(++attempts<30)window.setTimeout(reveal,100)};
    window.setTimeout(reveal,100);
  }

  function selectedUrl() {const backend=byId('gcBackend')?.value||'comfyui';return state.payload?.connection?.connections?.[backend]?.base_url||(backend==='comfyui'?'http://127.0.0.1:8188':'http://127.0.0.1:7861')}
  function render(payload) {
    state.payload=payload;
    const detail=byId('gcSummaryDetail');if(detail)detail.textContent=`${payload.mode.active_label} · ${payload.backend.label} · isolated data`;
    document.querySelectorAll('[data-gc-mode]').forEach(button=>{button.classList.toggle('active',button.dataset.gcMode===payload.mode.active_mode);button.disabled=!payload.local_request||state.restarting});
    const local=byId('gcLocal');if(local)local.hidden=payload.mode.active_mode!=='local';
    if(payload.mode.active_mode==='local'&&local){byId('gcBackend').value=payload.backend.id;byId('gcUrl').value=selectedUrl();byId('gcAllowLan').checked=Boolean(payload.connection.allow_non_loopback);byId('gcPreviewCount').value=payload.previews.limit;document.querySelectorAll('#gcLocal button,#gcLocal input,#gcLocal select').forEach(control=>control.disabled=!payload.local_request)}
    if(!payload.local_request)message('Connection and archive changes are available only on the PC running the ranker.');else if(payload.mode.active_mode==='novelai')message(payload.previews.novelai_note||'NovelAI live previews are unavailable through the current public image API.');
    renderPreviews(payload.previews);
  }
  async function refresh(){const payload=await api('/api/generation-control/status');render(payload);return payload}
  async function switchMode(target){if(!state.payload?.local_request||target===state.payload.mode.active_mode)return;state.restarting=true;message('Finishing any in-flight duel, saving this archive, then restarting...');document.querySelectorAll('[data-gc-mode]').forEach(button=>button.disabled=true);await api('/api/generation-control/mode',{method:'POST',body:JSON.stringify({mode:target})});const started=Date.now();while(Date.now()-started<20*60*1000){await new Promise(resolve=>setTimeout(resolve,1000));try{const health=await api('/api/public/health');if(health.generation_mode===target){location.reload();return}}catch{}}message('The restart is taking longer than expected. Reopen the launcher if it was not running.','error')}
  async function connectionAction(save){const payload={backend:byId('gcBackend').value,base_url:byId('gcUrl').value,allow_non_loopback:byId('gcAllowLan').checked};message(save?'Testing and saving endpoint...':'Testing endpoint...');const result=await api(save?'/api/generation-control/connection':'/api/generation-control/test',{method:'POST',body:JSON.stringify(payload)});message(result.message||'Connection succeeded.','success');await refresh()}
  async function scan(){message('Scanning common ComfyUI and Forge ports on this PC...');const result=await api('/api/generation-control/scan',{method:'POST',body:'{}'});const host=byId('gcEndpoints');host.innerHTML=(result.endpoints||[]).map((item,index)=>`<button type="button" data-gc-endpoint="${index}">${escapeHtml(item.label)} · ${escapeHtml(item.base_url)}<span>${escapeHtml(item.detail)}</span></button>`).join('')||'<span class="gc-note">No running endpoint was found on the common local ports. Enter an address manually.</span>';host.querySelectorAll('[data-gc-endpoint]').forEach(button=>button.addEventListener('click',async()=>{const item=result.endpoints[Number(button.dataset.gcEndpoint)];byId('gcBackend').value=item.backend;byId('gcUrl').value=item.base_url;await connectionAction(true)}));message(`${(result.endpoints||[]).length} endpoint(s) found.`,'success')}

  function wirePanel(){document.querySelectorAll('[data-gc-mode]').forEach(button=>button.addEventListener('click',()=>switchMode(button.dataset.gcMode).catch(error=>{state.restarting=false;message(error.message,'error')})));byId('gcBackend')?.addEventListener('change',()=>{byId('gcUrl').value=selectedUrl()});byId('gcTest')?.addEventListener('click',()=>connectionAction(false).catch(error=>message(error.message,'error')));byId('gcSave')?.addEventListener('click',()=>connectionAction(true).catch(error=>message(error.message,'error')));byId('gcScan')?.addEventListener('click',()=>scan().catch(error=>message(error.message,'error')));byId('gcPreviewSave')?.addEventListener('click',async()=>{try{const result=await api('/api/generation-control/preview-settings',{method:'POST',body:JSON.stringify({count:Number(byId('gcPreviewCount').value)})});message(result.message,'success');await refresh()}catch(error){message(error.message,'error')}})}

  function wireGlobal(){if(document.documentElement.dataset.gcGlobalWired==='1')return;document.documentElement.dataset.gcGlobalWired='1';document.addEventListener('click',event=>{const settings=event.target.closest?.('[data-gc-open-settings],#generation-settings-shortcut');if(settings){event.preventDefault();event.stopPropagation();openSettings()}},true)}
  function installAll(){wireGlobal();const panelReady=installPanel();installDuelStage();return panelReady}
  function start(){installAll();refresh().catch(error=>message(error.message,'error'));const observer=new MutationObserver(()=>installAll());observer.observe(document.querySelector('gradio-app')||document.body,{subtree:true,childList:true});state.previewTimer=setInterval(async()=>{if(document.visibilityState!=='visible'||state.restarting)return;try{renderPreviews(await api('/api/generation-control/previews'))}catch{}},650)}
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();
})();
</script>
""".replace(
    "__GENERATION_CONTROL_PANEL_HTML__",
    json.dumps(GENERATION_CONTROL_PANEL_HTML, ensure_ascii=False),
)

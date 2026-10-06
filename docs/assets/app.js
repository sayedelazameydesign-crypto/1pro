'use strict';
const $ = id => document.getElementById(id);
const icons = {
  compass:'<circle cx="12" cy="12" r="9"/><path d="m16 8-2.5 5.5L8 16l2.5-5.5Z"/>',
  book:'<path d="M12 6c-3-2-6-2-9-1v14c3-1 6-1 9 1 3-2 6-2 9-1V5c-3-1-6-1-9 1Z"/><path d="M12 6v14"/>',
  message:'<path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5H5l-3 2 1-6a8.5 8.5 0 1 1 18-4.5Z"/>',
  moon:'<path d="M20.7 13a9 9 0 1 1-9.7-9.7A7 7 0 0 0 20.7 13Z"/>',
  sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
  help:'<circle cx="12" cy="12" r="9"/><path d="M9 9a3 3 0 0 1 6 0c0 2-3 2-3 5"/><path d="M12 17h.01"/>',
  arrow:'<path d="M20 12H4m6-6-6 6 6 6"/>',
  send:'<path d="m21 3-7 18-4-7-7-4 18-7Zm0 0L10 14"/>',
  lock:'<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2"/>',
  layers:'<path d="m12 3 10 5-10 5L2 8l10-5Zm10 9-10 5-10-5m20 5-10 5-10-5"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  shield:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
  download:'<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  trash:'<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
  plus:'<path d="M12 4v16M4 12h16"/>',
  close:'<path d="m5 5 14 14M5 19 19 5"/>',
  pen:'<path d="m15 4 5 5L8 21H3v-5L15 4Zm-3 3 5 5"/>',
  code:'<path d="m8 6-6 6 6 6m8-12 6 6-6 6M14 3l-4 18"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l4 2"/>',
  layout:'<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 9v12"/>',
  mail:'<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 6 9 7 9-7"/>',
  chart:'<path d="M3 3v18h18M7 16v-5m5 5V6m5 10V9"/>',
  spark:'<path d="m12 3 2.3 6.7L21 12l-6.7 2.3L12 21l-2.3-6.7L3 12l6.7-2.3L12 3Z"/>',
  check:'<path d="m4 12 5 5L20 6"/>'
};
function icon(name){return `<svg class="icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name]||icons.spark}</svg>`;}
function injectIcons(){document.querySelectorAll('[data-icon]').forEach(el => el.innerHTML=icon(el.dataset.icon));}
function escapeHtml(value){return String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
const modes={guided:'شرح موجه',exercise:'تمرين تطبيقي',quiz:'اختبار'};
const state={me:null,skills:[],sessions:[],view:'discover',selected:null,current:null,busy:false};
let toastTimer;
function toast(text){$('toast').textContent=text;$('toast').classList.remove('hidden');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.add('hidden'),3500);}
function showError(id,error){$(id).textContent=error.message||String(error);$(id).classList.remove('hidden');}
// Backend adapter: talks to a real Waha server when data/config.json sets
// api_base; otherwise stays fully offline (static Pages mode, no external calls).
const backend={base:'',token:'',csrf:'',status:'off'};
try{backend.token=localStorage.getItem('waha-token')||'';}catch(error){}
async function loadBackend(){
 try{
  const response=await fetch('./data/config.json',{cache:'no-store'});
  const config=await response.json();
  const base=(config&&typeof config.api_base==='string')?config.api_base.trim():'';
  if(base)backend.base=base.replace(/\/+$/,'');
 }catch(error){}
}
// The free Render plan sleeps after ~15 idle minutes, so the first request can
// take ~50s. Give backend calls a long timeout, keep the user informed, and
// retry instead of failing fast.
const BACKEND_TIMEOUT_MS=80000;
function serverNote(text){const el=document.querySelector('.ai-status small');if(el)el.textContent=text;}
async function requestBackend(path,options){
 const controller=new AbortController();
 const timer=setTimeout(()=>controller.abort(),BACKEND_TIMEOUT_MS);
 try{
  return await fetch(backend.base+path,{...options,signal:controller.signal});
 }catch(error){
  if(error&&error.name==='AbortError')throw new Error('انتهت مهلة الاتصال بخادم واحة، وقد يكون في وضع خمول. أعد المحاولة بعد لحظات.');
  throw new Error('تعذّر الاتصال بخادم واحة. تحقق من اتصالك بالإنترنت ثم أعد المحاولة.');
 }finally{clearTimeout(timer);}
}
async function wakeBackend(){
 let lastError;
 for(let attempt=0;attempt<3;attempt++){
  if(attempt){
   serverNote('جارٍ إيقاظ خادم واحة… (محاولة '+(attempt+1)+' من 3)');
   await new Promise(resolve=>setTimeout(resolve,5000));
  }
  try{return await remoteMe();}catch(error){lastError=error;}
 }
 throw lastError;
}
async function registerVisitor(){
 const response=await requestBackend('/api/register',
  {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
 let data={};
 try{data=await response.json();}catch(error){}
 if(!response.ok)throw new Error(data.error||'تعذّر إنشاء هوية الزائر على الخادم.');
 backend.token=data.token;backend.csrf=data.csrf;
 try{localStorage.setItem('waha-token',data.token);}catch(error){}
}
async function remoteMe(){
 const headers={};
 if(backend.token)headers['Authorization']='Bearer '+backend.token;
 const response=await requestBackend('/api/me',{headers});
 if(!response.ok)throw new Error('تعذّر الاتصال بخادم واحة.');
 return response.json();
}
async function refreshCsrf(){
 try{
  const me=await remoteMe();
  if(me.csrf){backend.csrf=me.csrf;return true;}
  return false;
 }catch(error){return false;}
}
async function remote(path,body,retried){
 const headers={};
 if(backend.token)headers['Authorization']='Bearer '+backend.token;
 if(body!==undefined){headers['Content-Type']='application/json';headers['X-Waha-CSRF']=backend.csrf||'';}
 const options=body===undefined?{headers}:{method:'POST',headers,body:JSON.stringify(body)};
 const response=await requestBackend(path,options);
 if(!response.ok){
  let data={};
  try{data=await response.json();}catch(error){}
  const message=data.error||('تعذّر الاتصال بخادم واحة ('+response.status+').');
  if(!retried){
   if(response.status===401&&data.code==='sign_in_required'){await registerVisitor();return remote(path,body,true);}
   if(response.status===403&&data.code==='csrf_rejected'&&await refreshCsrf()){return remote(path,body,true);}
  }
  throw new Error(message);
 }
 return response.json();
}
async function api(path,body){
 if(!backend.base){
  if(body!==undefined)throw new Error('هذه نسخة Pages ثابتة؛ الحفظ وAI يعملان بعد ربط خادم واحة في ملف الإعداد.');
  if(path==='/api/me')return {authenticated:false,user:null,csrf:null,model:null,provider:null,ai_enabled:false,sample_data:true};
  if(path==='/api/sessions')return {sessions:[],installed_count:0,reply_count:0};
  if(path==='/api/agent/config')return {enabled:false,ai_mode:'off',tools:[],limits:{},providers:[]};
  if(path==='/api/agent/tasks')return {tasks:[]};
  if(path==='/api/agent/memory')return {memory:[]};
  if(path==='/api/skills'){
   const response=await fetch('./data/index.json');
   if(!response.ok)throw new Error('تعذّر تحميل فهرس المهارات.');
   const data=await response.json();
   return {skills:data.skills.map(s=>({...s,installed:false}))};
  }
  throw new Error('هذه الوظيفة غير متاحة في نسخة Pages.');
 }
 return remote(path,body);
}
function applyBackendUi(){
 if(!backend.base)return;
 const version=document.querySelector('.version');
 if(version)version.textContent='LIVE';
 const banner=document.querySelector('#identity-banner p');
 const aiSmall=document.querySelector('.ai-status small');
 if(backend.status==='unreachable'){
  if(banner)banner.textContent='تعذّر الوصول إلى خادم واحة الآن؛ الخادم المجاني ينام بعد فترة خمول، وأول طلب بعده قد يستغرق حتى دقيقة. حدّث الصفحة بعد قليل؛ البحث والتنزيل يعملان الآن.';
  if(aiSmall)aiSmall.textContent='الخادم غير متاح';
  return;
 }
 if(aiSmall){
  if(state.me.ai_enabled)aiSmall.textContent=(state.me.model||'Gemini')+' · جاهز';
  else aiSmall.textContent='غير مُفعّل على الخادم بعد';
 }
 const note=document.querySelector('.bottom-note p');
 if(note)note.innerHTML='هذه الواجهة متصلة بخادم واحة المستقل؛ المحادثات تُحفظ على الخادم، وتُرسل نصوص الأسئلة إلى خدمة AI عند تفعيلها.<br><span>المهارات عينات عربية، والتنزيل يحفظ ملف JSON فقط.</span>';
}

async function downloadSkill(id){
 try{
  const response=await fetch('./data/skills/'+id+'.json');
  if(!response.ok)throw new Error('تعذّر تنزيل المهارة.');
  const data=await response.json();
  const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});
  const url=URL.createObjectURL(blob);
  const link=document.createElement('a');
  link.href=url;link.download='waha-'+id+'.json';
  document.body.append(link);link.click();link.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
 }catch(error){toast(error.message);}
}

function authenticated(){if(state.me?.authenticated)return true;toast(backend.base?'تعذّر التحقق من خادم واحة؛ حدّث الصفحة وحاول مجدداً.':'الحفظ وAI يعملان بعد ربط خادم واحة في ملف الإعداد.');return false;}
function setTheme(theme){
 document.documentElement.dataset.theme=theme;
 localStorage.setItem('waha-theme',theme);
 $('theme-label').textContent=theme==='dark'?'المظهر الفاتح':'المظهر الداكن';
 $('theme-toggle').querySelector('[data-icon]').innerHTML=icon(theme==='dark'?'sun':'moon');
}
function switchView(view){
 if(state.busy){toast('انتظر انتهاء الرد أولاً.');return;}
 state.view=view;
 $('discovery-view').classList.toggle('hidden',view==='chats'||view==='conversation'||view==='workspace');
 $('chats-view').classList.toggle('hidden',view!=='chats');
 $('conversation-view').classList.toggle('hidden',view!=='conversation');
 $('workspace-view').classList.toggle('hidden',view!=='workspace');
 $('discovery-view').classList.toggle('library',view==='library');
 document.querySelectorAll('.nav-item').forEach(el=>el.classList.toggle('active',el.dataset.view===view));
 $('breadcrumb-title').textContent=({discover:'اكتشف المهارات',library:'مكتبتي',chats:'محادثاتي',conversation:'جلسة تعلّم',workspace:'مساحة العمل'})[view];
 $('catalog-title').textContent=view==='library'?'مهاراتك، في مكان واحد':'اختر مهارتك التالية';
 $('catalog-kicker').textContent=view==='library'?'جاهزة لسؤالك التالي':'ابدأ بشيء يثير فضولك';
 if(view==='discover'||view==='library')renderSkills();
 if(view==='chats')renderChats();
 if(view==='workspace')openWorkspace();
 window.scrollTo({top:0,behavior:'smooth'});
}
function renderSkills(){
 const q=$('search').value.trim().toLocaleLowerCase(),cat=$('category').value,difficulty=$('difficulty').value;
 const filtered=state.skills.filter(s=>(state.view!=='library'||s.installed)&&(!cat||s.category===cat)&&(!difficulty||s.difficulty===difficulty)&&(!q||[s.name,s.description,...s.tags].join(' ').toLocaleLowerCase().includes(q)));
 $('results-count').textContent=filtered.length+' مهارات';
 if(!filtered.length){
  $('skill-grid').innerHTML=`<div class="empty-state">${icon('book')}<h3>${state.view==='library'&&!q&&!cat&&!difficulty?'مكتبتك تبدأ بمهارة واحدة':'لم نجد مهارة بهذه الخيارات'}</h3><p>${state.view==='library'?'اكتشف مهارة ثم أضفها لمكتبتك أو ابدأ جلسة.':'جرّب كلمة أخرى أو أزل الفلاتر.'}</p></div>`;
  return;
 }
 $('skill-grid').innerHTML=filtered.map(s=>`<article class="skill-card"><div class="card-top"><div class="skill-icon ${escapeHtml(s.color)}">${icon(s.icon)}</div><span class="badge ${s.installed?'installed-tag':''}">${s.installed?'في مكتبتك':'مهارة تجريبية'}</span></div><h3>${escapeHtml(s.name)}</h3><p>${escapeHtml(s.description)}</p><div class="skill-meta"><span>${escapeHtml(s.category)}</span><span class="dot"></span><span>${escapeHtml(s.difficulty)}</span><span class="dot"></span><span>تعلّم تفاعلي</span></div><div class="card-footer"><button class="card-action" data-skill="${s.id}">استكشف المهارة${icon('arrow')}</button><button class="icon-button" data-download="${s.id}" aria-label="تنزيل ${escapeHtml(s.name)} JSON">${icon('download')}</button></div></article>`).join('');
}
// R2: retrieval happens on the server over R1's index. The page adds no ranking of
// its own -- it labels what it got. BROWSER_FALLBACK deliberately shows no list: the
// filtered cards below already are that result, and repeating them as "hits" would
// read like retrieval. NONE means no source, and the page says so instead of guessing.
const rag={mode:'NONE',results:[],note:'',payload:null,seq:0};
let ragTimer;
function ragBadge(mode){
 if(mode==='RAG_LOCAL')return '<span class="rag-badge rag-local">'+icon('layers')+'استرجاع من فهرس Waha</span>';
 if(mode==='BROWSER_FALLBACK')return '<span class="rag-badge rag-fallback">'+icon('search')+'تصفية المتصفح — ليست استرجاع RAG</span>';
 return '<span class="rag-badge">بلا مصدر</span>';
}
function ragRow(row){
 const score=typeof row.score==='number'?'<span class="rag-score">'+row.score.toFixed(2)+'</span>':'';
 const cite=row.citation?'<code class="rag-cite">'+escapeHtml(row.citation)+'</code>':'<span class="rag-cite rag-cite-none">بلا استشهاد</span>';
 const section=row.section?'<span class="rag-section">'+escapeHtml(row.section_label||row.section)+'</span>':'';
 const open=row.skill_id?'<button class="rag-open" data-rag-skill="'+escapeHtml(row.skill_id)+'">افتح المهارة'+icon('arrow')+'</button>':'';
 return '<div class="rag-hit"><div class="rag-hit-top"><strong>'+escapeHtml(row.title||row.skill_id||'')+'</strong>'+score+'</div><p>'+escapeHtml(row.snippet||'')+'</p><div class="rag-hit-foot">'+cite+section+open+'</div></div>';
}
function renderRag(){
 const panel=$('rag-panel');
 if(!panel)return;
 if(!rag.results.length&&!rag.note){panel.classList.add('hidden');panel.innerHTML='';return;}
 const meta=rag.payload&&rag.payload.index?rag.payload.index:null;
 const head='<div class="rag-head">'+ragBadge(rag.mode)
  +(rag.note?'<small class="rag-note">'+escapeHtml(rag.note)+'</small>':'')
  +(meta?'<small class="rag-meta">فهرس '+meta.chunks+' مقطعاً · '+(meta.content_status==='sample'?'بيانات عيّنة':'بيانات منشورة')+(meta.vector_gate?' · '+escapeHtml(meta.vector_gate):'')+'</small>':'')
  +'</div>';
 const hits=rag.results.length?'<div class="rag-hits">'+rag.results.map(ragRow).join('')+'</div>':'';
 panel.innerHTML=head+hits;
 panel.classList.remove('hidden');
}
async function runRag(){
 const query=$('search').value.trim();
 const mine=++rag.seq;
 if(!query||!window.WahaRag||!window.WahaRag.search){rag.mode='NONE';rag.results=[];rag.note='';rag.payload=null;renderRag();return;}
 const out=await window.WahaRag.search({query,apiBase:backend.base,k:5,fetchImpl:typeof fetch==='function'?fetch:undefined,
  browserFilter:needle=>{const key=needle.toLocaleLowerCase();return state.skills.filter(skill=>[skill.name,skill.description,...(skill.tags||[])].join(' ').toLocaleLowerCase().includes(key));}});
 if(mine!==rag.seq)return;  // a newer keystroke already answered; never show a stale list
 rag.mode=out.mode;rag.payload=out.payload||null;rag.note=out.note||'';
 rag.results=out.mode==='RAG_LOCAL'?(out.results||[]):[];
 if(out.mode==='BROWSER_FALLBACK')rag.note=(out.note?out.note+' — ':'')+'النتائج بالأسفل تصفية محلية للكتالوج، لا استرجاع من فهرس.';
 renderRag();
}
function scheduleRag(){clearTimeout(ragTimer);ragTimer=setTimeout(runRag,250);}

function renderChats(){
 if(!state.sessions.length){const emptyText=state.me?.authenticated?'ابدأ جلسة مع أي مهارة، وستجدها هنا لاحقاً.':(backend.base&&backend.status==='unreachable'?'الخادم غير متاح حالياً؛ حدّث الصفحة وحاول مجدداً.':'المحادثات ليست جزءاً من نسخة Pages الثابتة.');$('chats-list').innerHTML=`<div class="empty-state">${icon('message')}<h3>كل محادثة بداية جديدة</h3><p>${emptyText}</p><button class="secondary" id="chats-explore">اكتشف المهارات</button></div>`;return;}
 $('chats-list').innerHTML=state.sessions.map(s=>{
  const skill=state.skills.find(k=>k.id===s.skill_id);
  return `<button class="chat-card" data-session="${s.id}"><div class="skill-icon ${skill?.color||'mint'}">${icon(skill?.icon||'message')}</div><div class="chat-details"><h3>${escapeHtml(s.title)}</h3><p>${escapeHtml(s.skill_name)} · ${modes[s.mode]}</p></div><time>${new Date(s.updated_at*1000).toLocaleDateString('ar-EG',{month:'short',day:'numeric'})}</time>${icon('arrow')}</button>`;
 }).join('');
}
async function refresh(){
 const [skills,sessions]=await Promise.all([api('/api/skills'),api('/api/sessions')]);
 state.skills=skills.skills;state.sessions=sessions.sessions;
 $('skill-count').textContent=state.skills.length;
 $('installed-count').textContent=sessions.installed_count;
 $('chat-count').textContent=state.sessions.length;
 renderSkills();renderChats();
}
function openSkill(id){
 if(state.busy)return;
 const skill=state.skills.find(s=>s.id===id);
 if(!skill)return;
 state.selected=skill;
 $('dialog-error').classList.add('hidden');
 $('skill-detail').innerHTML=`<div class="dialog-skill-title"><div class="skill-icon ${skill.color}">${icon(skill.icon)}</div><h2>${escapeHtml(skill.name)}</h2></div><p class="dialog-description">${escapeHtml(skill.description)}</p><div class="dialog-tags"><span>${escapeHtml(skill.category)}</span><span>${escapeHtml(skill.difficulty)}</span><span>عينة محلية</span></div>`;
 $('install-skill').disabled=skill.installed||!state.me?.authenticated;
 $('install-skill').innerHTML=icon(skill.installed?'check':'plus')+(skill.installed?'موجودة في مكتبتك':'أضف لمكتبتي');
 $('start-session').disabled=!state.me?.authenticated;
 document.querySelector('input[name=mode][value=guided]').checked=true;
 $('skill-dialog').showModal();
}
async function openSession(id){
 if(state.busy)return;
 try{
  const result=await api('/api/sessions/'+id);
  state.current=result.session;
  renderConversation();
  switchView('conversation');
  $('message-input').value='';
  $('chat-error').classList.add('hidden');
 }catch(error){toast(error.message);}
}
function renderConversation(){
 const session=state.current,skill=state.skills.find(s=>s.id===session.skill_id);
 $('conversation-title').textContent=skill?.name||session.skill_name;
 $('conversation-mode').textContent=modes[session.mode]+' · محادثة خاصة محفوظة';
 if(!session.messages.length){
  $('messages').innerHTML=`<div class="chat-welcome">${icon('spark')}<h3>ابدأ بسؤال، واترك الباقي لفضولك.</h3><p>مساعدك جاهز للتعلّم معك بطريقة ${modes[session.mode]}.</p><button class="starter-prompt" id="starter-prompt">${escapeHtml(skill?.starter||'ساعدني أتعلم هذه المهارة.')}</button></div>`;
 }else{
  $('messages').innerHTML=session.messages.map(m=>`<div class="message ${m.role}"><span class="message-avatar">${m.role==='assistant'?'و':escapeHtml((state.me.user?.name||'أ')[0])}</span><div><span class="message-label">${m.role==='assistant'?'واحة · Gemini':'أنت'}</span><div class="message-content" dir="auto">${escapeHtml(m.content)}</div></div></div>`).join('');
 }
 $('messages').scrollTop=$('messages').scrollHeight;
}
async function sendMessage(event){
 event.preventDefault();
 if(state.busy||!state.current||!authenticated())return;
 const text=$('message-input').value.trim();
 if(!text)return;
 const currentId=state.current.id;
 state.busy=true;
 $('send-message').disabled=true;$('message-input').disabled=true;$('delete-chat').disabled=true;
 $('send-message').textContent='جارٍ الرد…';
 $('chat-error').classList.add('hidden');
 const thinking=document.createElement('div');thinking.className='thinking';thinking.textContent='واحة يفكّر معك…';$('messages').append(thinking);$('messages').scrollTop=$('messages').scrollHeight;
 try{
  const result=await api('/api/sessions/'+currentId+'/message',{text});
  state.current=result.session;
  $('message-input').value='';
  renderConversation();
  await refresh();
 }catch(error){showError('chat-error',error);}
 finally{
  thinking.remove();state.busy=false;
  $('send-message').disabled=false;$('message-input').disabled=false;$('delete-chat').disabled=false;
  $('send-message').innerHTML='إرسال'+icon('send');
  $('message-input').focus();
 }
}
// --- Agent workspace: goal -> plan -> tools -> approval -> artifacts --------
// Talks to /api/agent/* only when a Waha server is configured; on the static
// Pages build every control explains itself instead of failing.
const agent={config:null,task:null,history:[],events:[],timer:null,cursor:0,artifact:null,pollMs:1600,busy:false};
const agentStatus={queued:'في قائمة الانتظار',running:'ينفّذ الخطوة',awaiting_approval:'بانتظار موافقتك',completed:'مكتملة',failed:'لم تكتمل',cancelled:'ملغاة',interrupted:'مقاطَعة',expired:'انتهت مهلة الموافقة'};
const stepStatus={pending:'بانتظار',running:'جارٍ',done:'تم',failed:'تعذّر'};
function agentReady(){return !!(backend.base&&agent.config&&agent.config.enabled);}
async function loadAgentConfig(){
 try{agent.config=await api('/api/agent/config');}
 catch(error){agent.config={enabled:false,tools:[],limits:{},error:error&&error.message};}
 renderAgentChrome();
}
function renderAgentChrome(){
 const chip=$('agent-state-chip'),off=$('agent-off'),submit=$('agent-submit'),provider=$('agent-provider');
 const config=agent.config||{tools:[],limits:{}};
 const tools=config.tools||[],limits=config.limits||{};
 if(!backend.base){
  chip.textContent='غير متصل';
  off.innerHTML='<span data-icon="lock"></span><p>مساحة العمل تعمل بعد ربط خادم واحة في <code>docs/data/config.json</code> (حقل <code>api_base</code>). على نسخة Pages الثابتة يبقى الكتالوج والبحث والتنزيل.</p>';
  off.classList.remove('hidden');submit.disabled=true;injectIcons();return;
 }
 if(!agentReady()){
  chip.textContent='غير مفعّل';
  off.innerHTML='<span data-icon="spark"></span><p>الخادم متصل لكن طبقة الوكيل غير مفعّلة (لا <code>GEMINI_API_KEY</code> بعد). المحادثات والحفظ يعملان؛ مساحة العمل تظهر هنا فور تفعيل النموذج.</p>';
  off.classList.remove('hidden');submit.disabled=true;injectIcons();return;
 }
 off.classList.add('hidden');
 chip.textContent=(config.demo?'وضع العرض · ':'')+(config.model||'')+' · '+tools.length+' أدوات';
 provider.innerHTML=(config.providers||[]).filter(p=>p.allowed).map(p=>`<option value="${escapeHtml(p.key)}">${escapeHtml(p.label)}${p.requires_personal_connection?' (بوابة)':''}</option>`).join('');
 provider.classList.toggle('hidden',(config.providers||[]).filter(p=>p.allowed).length<2);
 $('agent-budget').textContent='حتى '+(limits.max_steps||'-')+' خطوات · '+(limits.max_tool_calls||'-')+' استدعاء أداة · مهلة '+(limits.deadline_seconds||'-')+' ثانية · حدّ النموذج مشترك بين كل الزوّار';
 $('agent-tools-list').innerHTML=tools.map(t=>`<span class="agent-tool" title="${escapeHtml(t.description)}">${escapeHtml(t.name)}${t.requires_approval?'<em>موافقة</em>':''}</span>`).join('');
 if(config.demo)$('agent-demo-note').classList.remove('hidden');
}
// System components panel: it asks the server for /health and /api/agent/config through the
// same adapter every other view uses, then renders whatever WahaComponents.build() decides.
// Nothing here invents a status: a card the server did not answer for stays "غير معروف",
// and the panel is the only place the page says "LIVE" -- never the catalogue view.
const components={data:null,loading:false};
async function loadComponents(){
 if(!backend.base){components.data=null;renderComponents();return;}
 components.loading=true;renderComponents();
 const cached=(agent.config&&!agent.config.error&&agent.config.execution)?agent.config:null;
 const answers=await Promise.all([
  remote('/health').catch(()=>null),
  cached?Promise.resolve(cached):remote('/api/agent/config').catch(()=>null)
 ]);
 const [health,config]=answers;
 components.data=window.WahaComponents.build({apiBase:backend.base,health:health,agentConfig:config,ragMode:rag.mode,
  error:(health||config)?null:'تعذّر الوصول إلى خادم واحة: لم يصل /health ولا /api/agent/config.'});
 components.loading=false;renderComponents();
}
function renderComponents(){
 const grid=$('components-grid'),chip=$('components-chip'),foot=$('components-foot');
 if(!grid)return;
 const built=components.data||window.WahaComponents.build({apiBase:backend.base,pending:components.loading});
 if(chip)chip.textContent=({LIVE:'حالة حيّة',LOADING:'جارٍ القراءة…',OFFLINE:'غير متصل',UNREACHABLE:'تعذّر الوصول'})[built.mode]||built.mode;
 grid.innerHTML=built.cards.map(card=>`<div class="component-card ${escapeHtml(card.state)}"><div class="component-head"><strong>${escapeHtml(card.name)}</strong><span class="component-status ${escapeHtml(card.state)}">${escapeHtml(card.label)}</span></div><p class="component-role">${escapeHtml(card.role)}</p><p class="component-detail">${escapeHtml(card.detail)}</p></div>`).join('');
 if(foot)foot.textContent=built.note;
}
async function loadAgentHistory(){
 if(!agentReady())return;
 try{const data=await api('/api/agent/tasks');agent.history=data.tasks||[];}
 catch(error){agent.history=[];}
 renderAgentHistory();
}function renderAgentHistory(){
 const box=$('agent-history');
 if(!agent.history.length){box.innerHTML='<p class="agent-empty">لا مهام بعد. اكتب هدفاً لتبدأ الواحة بالتخطيط.</p>';return;}
 box.innerHTML='<span class="tiny-label">مهام سابقة</span>'+agent.history.slice(0,8).map(t=>`<button class="agent-history-item" data-task="${escapeHtml(t.id)}"><span class="agent-pill ${escapeHtml(t.status)}">${escapeHtml(agentStatus[t.status]||t.status)}</span><strong>${escapeHtml(t.goal.slice(0,70))}</strong><small>${new Date(t.updated_at*1000).toLocaleString('ar-EG',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}</small></button>`).join('');
}
async function openAgentTask(id){
 try{
  const data=await api('/api/agent/tasks/'+id);
  agent.task=data.task;agent.events=[];agent.cursor=0;
  $('agent-active').classList.remove('hidden');
  renderAgentTask();startAgentPolling();
 }catch(error){toast(error.message);}
}
function startAgentPolling(){
 stopAgentPolling();
 agent.timer=setInterval(pollAgent,agent.pollMs);
 pollAgent();
}
function stopAgentPolling(){if(agent.timer){clearInterval(agent.timer);agent.timer=null;}}
async function pollAgent(){
 if(!agent.task||agent.busy)return;
 agent.busy=true;
 try{
  const id=agent.task.id;
  const [detail,feed]=await Promise.all([api('/api/agent/tasks/'+id),api(`/api/agent/tasks/${id}/events?cursor=${agent.cursor}`)]);
  agent.task=detail.task;
  if(feed.events&&feed.events.length){agent.cursor=feed.cursor;agent.events=agent.events.concat(feed.events).slice(-40);}
  renderAgentTask();
  if(!detail.task.active){
   stopAgentPolling();
   agent.busy=false;
   await Promise.all([loadAgentHistory(),loadMemory()]);
   return;
  }
 }catch(error){/* الخادم قد ينام: نستمر في المحاولة بصمت */}
 finally{agent.busy=false;}
}
function renderAgentTask(){
 const task=agent.task;
 if(!task)return;
 $('agent-active').classList.remove('hidden');
 $('agent-task-goal').textContent=task.goal.slice(0,140);
 $('agent-task-status').textContent=agentStatus[task.status]||task.status;
 $('agent-task-status').className='agent-pill '+escapeHtml(task.status);
 const steps=task.steps||[];
 const done=steps.filter(s=>s.status==='done').length;
 $('agent-progress-bar').style.width=task.active&&steps.length?Math.max(6,Math.round(done/steps.length*100))+'%':(task.active?'8%':'100%');
 $('agent-steps').innerHTML=steps.length?steps.map(s=>`<li class="agent-step ${escapeHtml(s.status)}"><span class="agent-step-dot"></span><div><strong>${escapeHtml(s.title)}</strong>${s.detail?`<p>${escapeHtml(s.detail.slice(0,220))}</p>`:''}${s.output?`<div class="agent-step-out">${escapeHtml(String(s.output).slice(0,700))}</div>`:''}</div></li>`).join(''):'<li class="agent-empty-step">تخطّط الواحة لهذه المهمة الآن…</li>';
 $('agent-plan').innerHTML=(task.plan||[]).length?'<span class="tiny-label">الخطة</span>'+(task.plan||[]).map((p,i)=>`<span class="agent-plan-step">${i+1}. ${escapeHtml(typeof p==='string'?p:p.title)}</span>`).join(''):'';
 renderAgentApprovals(task);
 renderAgentArtifacts(task);
 $('agent-events').innerHTML=agent.events.map(e=>`<li><code>${escapeHtml(e.type)}</code><span>${escapeHtml(shortPayload(e.payload))}</span></li>`).join('');
 const usage=task.usage||{};
 $('agent-usage').textContent=(task.ai_calls||0)+' استدعاء نموذج · '+(task.tool_calls||0)+' أداة · '+(usage.prompt_tokens||0)+'+'+(usage.completion_tokens||0)+' رمزاً'+(task.error?' · '+task.error:'');
 const active=!!task.active;
 $('agent-cancel').classList.toggle('hidden',!active);
 $('agent-stream-hint').textContent=task.status==='awaiting_approval'?'هذه الخطوة موقوفة حتى توافق أو ترفض.':(active?'تُحدَّث الحالة كل ثانيتين.':'انتهت المهمة.');
}
function shortPayload(payload){
 if(!payload||typeof payload!=='object')return '';
 const parts=[];
 for(const key of['title','tool','reason','error','name','goal','report','step_id','call_id']){
  const value=payload[key];
  if(typeof value==='string'&&value.trim())parts.push(value.trim().slice(0,90));
 }
 return parts.join(' · ');
}
function renderAgentApprovals(task){
 const box=$('agent-approvals');
 const pending=(task.calls||[]).filter(c=>c.status==='awaiting_approval');
 if(!pending.length){box.classList.add('hidden');box.innerHTML='';return;}
 box.classList.remove('hidden');
 box.innerHTML=pending.map(call=>`<div class="agent-approval"><div><strong>إجراء يحتاج موافقتك</strong><p><code>${escapeHtml(call.tool)}</code> <span>بوسائط: ${escapeHtml(JSON.stringify(call.args).slice(0,180))}</span></p><small>لن يُنفَّذ شيء قبل ردّك، وتنتهي المهلة تلقائياً فيُلغى الطلب.</small></div><div class="agent-approval-actions"><button class="primary" data-approve="${escapeHtml(call.id)}" data-value="1">سماح</button><button class="secondary" data-approve="${escapeHtml(call.id)}" data-value="0">رفض</button></div></div>`).join('');
}
function renderAgentArtifacts(task){
 const box=$('agent-artifacts');
 const list=task.artifacts||[];
 if(!list.length){box.innerHTML='<p class="agent-empty">لا ملفات في هذه المهمة بعد.</p>';return;}
 box.innerHTML=list.map(a=>`<button class="agent-artifact ${agent.artifact&&agent.artifact.id===a.id?'active':''}" data-artifact="${a.id}"><span data-icon="layers"></span><div><strong>${escapeHtml(a.name)}</strong><small>${escapeHtml(a.kind)} · ${a.bytes} حرفاً</small></div></button>`).join('');
 injectIcons();
}
async function decideApproval(callId,approve){
 if(!agent.task)return;
 try{
  await api('/api/agent/tasks/'+agent.task.id+'/approve',{call_id:callId,approve});
  toast(approve?'سُمح بتنفيذ الأداة.':'رُفض تنفيذ الأداة.');
  await pollAgent();
 }catch(error){toast(error.message);}
}
async function cancelAgentTask(){
 if(!agent.task)return;
 try{await api('/api/agent/tasks/'+agent.task.id+'/cancel',{});await pollAgent();toast('أُلغيت المهمة.');}
 catch(error){toast(error.message);}
}
async function deleteAgentTask(){
 if(!agent.task||!confirm('حذف هذه المهمة وسجلها نهائياً؟'))return;
 const id=agent.task.id;
 try{
  await api('/api/agent/tasks/'+id+'/delete',{});
  stopAgentPolling();agent.task=null;agent.events=[];agent.artifact=null;
  $('agent-active').classList.add('hidden');
  $('artifact-frame').removeAttribute('srcdoc');
  await loadAgentHistory();
  toast('حُذفت المهمة.');
 }catch(error){toast(error.message);}
}
async function previewArtifact(id){
 try{
  const data=await api('/api/agent/artifacts/'+id);
  agent.artifact=data.artifact;
  paintPreview();
  renderAgentArtifacts(agent.task||{artifacts:[data.artifact]});
 }catch(error){toast(error.message);}
}
function paintPreview(){
 const artifact=agent.artifact,frame=$('artifact-frame');
 if(!artifact){frame.removeAttribute('srcdoc');return;}
 const runnable=$('artifact-run').checked;
 // Empty `sandbox` = the strictest isolation (no same-origin, no top navigation).
 // Scripts stay off unless the visitor explicitly turns them on.
 frame.setAttribute('sandbox',runnable?'allow-scripts':'');
 const shell='<style>body{margin:16px;font:15px/1.7 system-ui,"Noto Sans Arabic",sans-serif;color:#1c2b22;background:#fff;direction:rtl}pre{white-space:pre-wrap;word-break:break-word}code{font-family:ui-monospace,monospace}</style>';
 const body=artifact.kind==='html'?artifact.content:'<pre>'+escapeHtml(artifact.content)+'</pre>';
 frame.srcdoc=shell+body;
 $('artifact-run-row').classList.toggle('hidden',artifact.kind!=='html');
 $('artifact-name').textContent=artifact.name;
}
async function downloadArtifact(){
 const artifact=agent.artifact;
 if(!artifact){toast('اختر ملفاً أولاً.');return;}
 const blob=new Blob([artifact.content],{type:'text/plain;charset=utf-8'});
 const url=URL.createObjectURL(blob);
 const link=document.createElement('a');
 link.href=url;link.download=artifact.name;
 document.body.append(link);link.click();link.remove();
 setTimeout(()=>URL.revokeObjectURL(url),1000);
}
async function copyArtifact(){
 if(!agent.artifact)return;
 try{await navigator.clipboard.writeText(agent.artifact.content);toast('نُسخ المحتوى.');}
 catch{toast('تعذّر النسخ من المتصفح.');}
}
async function loadMemory(){
 if(!agentReady())return;
 try{const data=await api('/api/agent/memory');agent.memory=data.memory||[];}
 catch(error){agent.memory=[];}
 renderMemory();
}
function renderMemory(){
 const box=$('memory-list');
 if(!box)return;
 if(!agent.memory||!agent.memory.length){box.innerHTML='<li class="agent-empty">لا ملاحظات بعد؛ أضف تفضيلاً لتتعرّف عليك الواحة في المهام القادمة.</li>';return;}
 box.innerHTML=agent.memory.map(m=>`<li><span class="memory-kind">${escapeHtml(m.kind==='goal'?'هدف':m.kind==='preference'?'تفضيل':'ملاحظة')}</span><p>${escapeHtml(m.content)}</p><button class="icon-button tiny-btn" data-memory="${m.id}" aria-label="حذف الملاحظة">${icon('trash')}</button></li>`).join('');
}
async function startAgentTask(event){
 event.preventDefault();
 if(agent.busy)return;
 if(!authenticated())return;
 const goal=$('agent-goal').value.trim();
 if(goal.length<8){showError('agent-error',{message:'اكتب هدفاً أوضح (٨ أحرف على الأقل).'});return;}
 $('agent-error').classList.add('hidden');
 $('agent-submit').disabled=true;$('agent-submit').innerHTML='جارٍ الإنشاء…';
 try{
  const body={goal};
  const provider=$('agent-provider');
  if(provider&&provider.value)body.provider=provider.value;
  const data=await api('/api/agent/tasks',body);
  agent.task=data.task;agent.events=[];agent.cursor=0;
  $('agent-goal').value='';
  renderAgentTask();startAgentPolling();await loadAgentHistory();
 }catch(error){showError('agent-error',error);}
 finally{$('agent-submit').disabled=false;$('agent-submit').innerHTML='ابدأ المهمة'+icon('send');}
}
$('agent-approvals').addEventListener('click',e=>{
 const button=e.target.closest('[data-approve]');
 if(button)decideApproval(button.dataset.approve,button.dataset.value==='1');
});
$('agent-artifacts').addEventListener('click',e=>{
 const button=e.target.closest('[data-artifact]');
 if(button)previewArtifact(button.dataset.artifact);
});
$('agent-history').addEventListener('click',e=>{
 const button=e.target.closest('[data-task]');
 if(button)openAgentTask(button.dataset.task);
});
$('memory-list').addEventListener('click',async e=>{
 const button=e.target.closest('[data-memory]');
 if(!button)return;
 try{await api('/api/agent/memory/'+button.dataset.memory+'/delete',{});await loadMemory();}
 catch(error){toast(error.message);}
});
$('memory-form').addEventListener('submit',async event=>{
 event.preventDefault();
 if(!authenticated())return;
 const content=$('memory-input').value.trim();
 if(content.length<3){toast('الملاحظة قصيرة جداً.');return;}
 try{
  await api('/api/agent/memory',{content,kind:$('memory-kind').value});
  $('memory-input').value='';await loadMemory();
 }catch(error){toast(error.message);}
});
$('agent-cancel').addEventListener('click',cancelAgentTask);
$('agent-delete').addEventListener('click',deleteAgentTask);
$('artifact-run').addEventListener('change',paintPreview);
$('artifact-download').addEventListener('click',downloadArtifact);
$('artifact-copy').addEventListener('click',copyArtifact);
$('agent-form').addEventListener('submit',startAgentTask);

async function openWorkspace(){
 await loadAgentConfig();
 await loadComponents();
 await loadAgentHistory();
 await loadMemory();
 if(agent.task)startAgentPolling();
 if(!backend.base||!agentReady())stopAgentPolling();
 injectIcons();
}
document.querySelectorAll('[data-view]').forEach(el=>el.addEventListener('click',()=>switchView(el.dataset.view)));
if($('components-refresh'))$('components-refresh').addEventListener('click',loadComponents);
document.querySelectorAll('.close-dialog').forEach(el=>el.addEventListener('click',()=>el.closest('dialog').close()));
document.querySelectorAll('dialog').forEach(d=>d.addEventListener('click',e=>{if(e.target===d){const r=d.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)d.close();}}));
$('theme-toggle').addEventListener('click',()=>setTheme(document.documentElement.dataset.theme==='dark'?'light':'dark'));
$('help-button').addEventListener('click',()=>$('help-dialog').showModal());
$('explore-button').addEventListener('click',()=>$('catalog').scrollIntoView({behavior:'smooth'}));
['search','category','difficulty'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',()=>{renderSkills();if(id==='search')scheduleRag();}));
$('rag-panel')&&$('rag-panel').addEventListener('click',event=>{const open=event.target.closest('[data-rag-skill]');if(open)openSkill(open.dataset.ragSkill);});
scheduleRag();
$('skill-grid').addEventListener('click',e=>{const open=e.target.closest('[data-skill]'),download=e.target.closest('[data-download]');if(open)openSkill(open.dataset.skill);if(download)downloadSkill(download.dataset.download);});
$('chats-list').addEventListener('click',e=>{const button=e.target.closest('[data-session]');if(button)openSession(button.dataset.session);if(e.target.closest('#chats-explore'))switchView('discover');});
$('download-skill').addEventListener('click',()=>{if(state.selected)downloadSkill(state.selected.id);});
$('install-skill').addEventListener('click',async()=>{
 if(!authenticated()||!state.selected)return;
 $('install-skill').disabled=true;
 try{await api('/api/skills/'+state.selected.id+'/install',{});await refresh();$('install-skill').innerHTML=icon('check')+'موجودة في مكتبتك';toast('أُضيفت المهارة إلى مكتبتك');}catch(error){showError('dialog-error',error);$('install-skill').disabled=false;}
});
$('start-session').addEventListener('click',async()=>{
 if(!authenticated()||!state.selected)return;
 $('start-session').disabled=true;
 try{
  const result=await api('/api/sessions',{skill_id:state.selected.id,mode:document.querySelector('input[name=mode]:checked').value});
  $('skill-dialog').close();await refresh();await openSession(result.session.id);
 }catch(error){showError('dialog-error',error);}
 finally{$('start-session').disabled=!state.me?.authenticated;}
});
$('chat-back').addEventListener('click',()=>switchView('chats'));
$('export-chat').addEventListener('click',async()=>{
 if(!state.current)return;
 if(!backend.base){window.location.href='/api/sessions/'+state.current.id+'/export';return;}
 try{
  const headers={};
  if(backend.token)headers['Authorization']='Bearer '+backend.token;
  const response=await fetch(backend.base+'/api/sessions/'+state.current.id+'/export',{headers});
  if(!response.ok)throw new Error('تعذّر تصدير المحادثة.');
  const blob=await response.blob();
  const url=URL.createObjectURL(blob);
  const link=document.createElement('a');
  link.href=url;link.download='waha-conversation.json';
  document.body.append(link);link.click();link.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
 }catch(error){toast(error.message);}
});
$('delete-chat').addEventListener('click',async()=>{
 if(!state.current||state.busy)return;
 if(!confirm('حذف هذه المحادثة نهائياً؟ لا يمكن استرجاعها.'))return;
 try{await api('/api/sessions/'+state.current.id+'/delete',{});state.current=null;await refresh();switchView('chats');toast('تم حذف المحادثة');}catch(error){toast(error.message);}
});
$('messages').addEventListener('click',e=>{if(e.target.closest('#starter-prompt')){$('message-input').value=state.skills.find(s=>s.id===state.current.skill_id).starter;$('message-input').focus();}});
$('message-form').addEventListener('submit',sendMessage);
$('message-input').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();$('message-form').requestSubmit();}});
async function init(){
 injectIcons();
 try{setTheme(localStorage.getItem('waha-theme')||'light');}catch{document.documentElement.dataset.theme='light';}
 try{
  await loadBackend();
  if(backend.base){
   const notice=setTimeout(()=>serverNote('جارٍ الاتصال بخادم واحة… أول طلب بعد الخمول قد يستغرق حتى دقيقة.'),2500);
   try{
    if(!backend.token)await registerVisitor();
    state.me=await wakeBackend();
    backend.status='online';
   }catch(error){
    state.me={authenticated:false,user:null,csrf:null,model:null,provider:null,ai_enabled:false};
    backend.status='unreachable';
    toast('تعذّر الوصول إلى خادم واحة الآن؛ إن كان في وضع خمول فسيستغرق أول طلب حتى دقيقة. حدّث الصفحة بعد قليل.');
   }finally{clearTimeout(notice);}
  }else{
   state.me=await api('/api/me');
  }
  applyBackendUi();
  await loadAgentConfig();
  $('identity-banner').classList.toggle('hidden',state.me.authenticated);
  if(state.me.authenticated){
   $('user-name').textContent=state.me.user.name;
   $('user-avatar').textContent=state.me.user.name[0];
   $('user-detail').textContent='مكتبة ومحادثات خاصة';
  }
  await refresh();
 }catch(error){
  $('skill-grid').textContent='تعذّر تحميل التطبيق. حدّث الصفحة وحاول مجدداً.';
  toast(error.message);
 }
}
init();
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
const providerLabel=key=>key==='nvidia'?'NVIDIA':'Gemini';
const modes={guided:'شرح موجه',exercise:'تمرين تطبيقي',quiz:'اختبار'};
const state={me:null,skills:[],sessions:[],view:'discover',selected:null,current:null,busy:false};
let toastTimer;
function toast(text){$('toast').textContent=text;$('toast').classList.remove('hidden');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.add('hidden'),3500);}
function showError(id,error){$(id).textContent=error.message||String(error);$(id).classList.remove('hidden');}
async function api(path,body){
 const options={headers:{}};
 if(body!==undefined){options.method='POST';options.headers={'Content-Type':'application/json','X-Waha-CSRF':state.me?.csrf||''};options.body=JSON.stringify(body);}
 const response=await fetch(path,options);
 const data=await response.json().catch(()=>({error:'تعذّر قراءة استجابة الخادم.'}));
 if(!response.ok)throw new Error(data.error||'تعذّر إكمال الطلب.');
 return data;
}
function authenticated(){if(state.me?.authenticated)return true;toast('افتح التطبيق من PromptQL لتفعيل هذه الميزة.');return false;}
function setTheme(theme){
 document.documentElement.dataset.theme=theme;
 localStorage.setItem('waha-theme',theme);
 $('theme-label').textContent=theme==='dark'?'المظهر الفاتح':'المظهر الداكن';
 $('theme-toggle').querySelector('[data-icon]').innerHTML=icon(theme==='dark'?'sun':'moon');
}
function switchView(view){
 if(state.busy){toast('انتظر انتهاء الرد أولاً.');return;}
 state.view=view;
 $('discovery-view').classList.toggle('hidden',view==='chats'||view==='conversation');
 $('chats-view').classList.toggle('hidden',view!=='chats');
 $('conversation-view').classList.toggle('hidden',view!=='conversation');
 $('discovery-view').classList.toggle('library',view==='library');
 document.querySelectorAll('.nav-item').forEach(el=>el.classList.toggle('active',el.dataset.view===view));
 $('breadcrumb-title').textContent=({discover:'اكتشف المهارات',library:'مكتبتي',chats:'محادثاتي',conversation:'جلسة تعلّم'})[view];
 $('catalog-title').textContent=view==='library'?'مهاراتك، في مكان واحد':'اختر مهارتك التالية';
 $('catalog-kicker').textContent=view==='library'?'جاهزة لسؤالك التالي':'ابدأ بشيء يثير فضولك';
 if(view==='discover'||view==='library')renderSkills();
 if(view==='chats')renderChats();
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
function renderChats(){
 if(!state.sessions.length){$('chats-list').innerHTML=`<div class="empty-state">${icon('message')}<h3>كل محادثة بداية جديدة</h3><p>${state.me?.authenticated?'ابدأ جلسة مع أي مهارة، وستجدها هنا لاحقاً.':'افتح التطبيق من PromptQL لحفظ محادثاتك.'}</p><button class="secondary" id="chats-explore">اكتشف المهارات</button></div>`;return;}
 $('chats-list').innerHTML=state.sessions.map(s=>{
  const skill=state.skills.find(k=>k.id===s.skill_id);
  return `<button class="chat-card" data-session="${s.id}"><div class="skill-icon ${skill?.color||'mint'}">${icon(skill?.icon||'message')}</div><div class="chat-details"><h3>${escapeHtml(s.title)}</h3><p>${escapeHtml(s.skill_name)} · ${modes[s.mode]} · ${providerLabel(s.provider)}</p></div><time>${new Date(s.updated_at*1000).toLocaleDateString('ar-EG',{month:'short',day:'numeric'})}</time>${icon('arrow')}</button>`;
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
 $('provider-choice').value='gemini';
 $('free-confirm').checked=false;
 updateProviderChoice();
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
function renderConversation(streamLast){
 // Any full render invalidates an in-flight stream: a tick may only write into
 // the bubble this exact render produced, never into a newer one.
 answerSeq++;
 const session=state.current,skill=state.skills.find(s=>s.id===session.skill_id);
 $('conversation-title').textContent=skill?.name||session.skill_name;
 $('conversation-mode').textContent=modes[session.mode]+' · '+providerLabel(session.provider)+' · محادثة خاصة محفوظة';
 $('composer-provider').textContent=providerLabel(session.provider)+' · حفظ تلقائي · 30 طلباً / ساعة';
 $('ai-disclaimer').textContent='يُرسل سؤالك وسياق هذه الجلسة فقط إلى '+providerLabel(session.provider)+' لإنشاء الرد. راجع الإجابات؛ لا يوجد تحويل تلقائي.';
 if(!session.messages.length){
  $('messages').innerHTML=`<div class="chat-welcome">${icon('spark')}<h3>ابدأ بسؤال، واترك الباقي لفضولك.</h3><p>مساعدك جاهز للتعلّم معك بطريقة ${modes[session.mode]}.</p><button class="starter-prompt" id="starter-prompt">${escapeHtml(skill?.starter||'ساعدني أتعلم هذه المهارة.')}</button></div>`;
 }else{
  const messages=session.messages;
  $('messages').innerHTML=messages.map((m,i)=>{
   const streaming=streamLast&&i===messages.length-1&&m.role==='assistant';
   return `<div class="message ${m.role}"><span class="message-avatar">${m.role==='assistant'?'و':escapeHtml((state.me.user?.name||'أ')[0])}</span><div><span class="message-label">${m.role==='assistant'?'واحة · '+providerLabel(m.provider||session.provider):'أنت'}</span><div class="message-content" dir="auto">${streaming?'':escapeHtml(m.content)}</div></div></div>`;
  }).join('');
 }
 $('messages').scrollTop=$('messages').scrollHeight;
}
// --- Progressive answers + source cards ----------------------------------
// The reply is already saved server-side when it arrives; streaming only paces
// its appearance, so a dropped frame can never lose content. Sources come from
// one extra same-origin GET /api/search over R1's index (read-only, no model
// call) and are built by window.WahaAnswer, which only cards RAG_LOCAL rows
// with citations. (Same-origin here: this UI is served by Flask itself, so no
// api_base gate like the Pages copy.)
let answerSeq=0;
const STREAM_TICK_MS=28,SOURCES_TIMEOUT_MS=6000;
function reducedMotion(){try{return matchMedia('(prefers-reduced-motion: reduce)').matches;}catch(error){return false;}}
function lastAssistantBubble(){
 const nodes=document.querySelectorAll('#messages .message.assistant .message-content');
 return nodes.length?nodes[nodes.length-1]:null;
}
function streamAnswer(fullText,onDone){
 const done=()=>{if(typeof onDone==='function')onDone();};
 const bubble=lastAssistantBubble();
 if(!bubble||!window.WahaAnswer||reducedMotion()||!fullText){
  if(bubble){bubble.textContent=fullText||'';}
  else{renderConversation();}
  done();
  return;
 }
 const mine=answerSeq;
 const frames=window.WahaAnswer.slices(fullText);
 let index=0;
 bubble.classList.add('typing');
 const finish=()=>{
  if(mine!==answerSeq)return;
  bubble.textContent=fullText;
  bubble.classList.remove('typing');
  bubble.removeEventListener('click',finish);
  done();
 };
 bubble.addEventListener('click',finish);
 const tick=()=>{
  if(mine!==answerSeq)return;  // a newer render owns the bubbles now
  if(index>=frames.length){finish();return;}
  bubble.textContent=frames[index++];
  $('messages').scrollTop=$('messages').scrollHeight;
  setTimeout(tick,STREAM_TICK_MS);
 };
 tick();
}
async function attachSources(question){
 const mine=answerSeq;
 if(!window.WahaAnswer||!question)return;
 let payload=null,indexMissing=false;
 try{
  const controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(),SOURCES_TIMEOUT_MS);
  try{
   const response=await fetch('/api/search?q='+encodeURIComponent(question)+'&k='+window.WahaAnswer.SOURCES_MAX,{headers:{Accept:'application/json'},signal:controller.signal});
   if(response.status===503){
    try{payload=await response.json();}catch(error){payload=null;}
    indexMissing=!!(payload&&payload.code==='rag_index_missing');
    payload=null;
   }else if(response.ok){
    try{payload=await response.json();}catch(error){payload=null;}
   }
  }finally{clearTimeout(timer);}
 }catch(error){return;}  // transport failure: the answer already arrived; stay silent
 if(mine!==answerSeq)return;  // the visitor moved on; never append to a foreign bubble
 const bubble=lastAssistantBubble();
 if(!bubble)return;
 const out=window.WahaAnswer.cards(payload);
 let html=out.html;
 if(out.state==='ignored'&&indexMissing)html=window.WahaAnswer.unavailableNote();
 if(!html)return;
 const host=document.createElement('div');
 host.innerHTML=html;
 if(host.firstChild)bubble.after(host.firstChild);
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
document.querySelectorAll('[data-view]').forEach(el=>el.addEventListener('click',()=>switchView(el.dataset.view)));
document.querySelectorAll('.close-dialog').forEach(el=>el.addEventListener('click',()=>el.closest('dialog').close()));
document.querySelectorAll('dialog').forEach(d=>d.addEventListener('click',e=>{if(e.target===d){const r=d.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)d.close();}}));
$('theme-toggle').addEventListener('click',()=>setTheme(document.documentElement.dataset.theme==='dark'?'light':'dark'));
$('help-button').addEventListener('click',()=>$('help-dialog').showModal());
$('explore-button').addEventListener('click',()=>$('catalog').scrollIntoView({behavior:'smooth'}));
['search','category','difficulty'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',renderSkills));
$('skill-grid').addEventListener('click',e=>{const open=e.target.closest('[data-skill]'),download=e.target.closest('[data-download]');if(open)openSkill(open.dataset.skill);if(download)window.location.href='/api/skills/'+download.dataset.download+'/download';});
$('chats-list').addEventListener('click',e=>{const button=e.target.closest('[data-session]');if(button)openSession(button.dataset.session);if(e.target.closest('#chats-explore'))switchView('discover');});
$('download-skill').addEventListener('click',()=>{if(state.selected)window.location.href='/api/skills/'+state.selected.id+'/download';});
$('install-skill').addEventListener('click',async()=>{
 if(!authenticated()||!state.selected)return;
 $('install-skill').disabled=true;
 try{await api('/api/skills/'+state.selected.id+'/install',{});await refresh();$('install-skill').innerHTML=icon('check')+'موجودة في مكتبتك';toast('أُضيفت المهارة إلى مكتبتك');}catch(error){showError('dialog-error',error);$('install-skill').disabled=false;}
});

function updateProviderChoice(){
 const nvidia=$('provider-choice').value==='nvidia';
 $('nvidia-notice').classList.toggle('hidden',!nvidia);
 $('start-session').disabled=!state.me?.authenticated||(nvidia&&!$('free-confirm').checked);
}
$('provider-choice').addEventListener('change',updateProviderChoice);
$('free-confirm').addEventListener('change',updateProviderChoice);

$('start-session').addEventListener('click',async()=>{
 if(!authenticated()||!state.selected)return;
 $('start-session').disabled=true;
 try{
  const result=await api('/api/sessions',{skill_id:state.selected.id,mode:document.querySelector('input[name=mode]:checked').value,provider:$('provider-choice').value,free_endpoint_confirmed:$('free-confirm').checked});
  $('skill-dialog').close();await refresh();await openSession(result.session.id);
 }catch(error){showError('dialog-error',error);}
 finally{updateProviderChoice();}
});
$('chat-back').addEventListener('click',()=>switchView('chats'));
$('export-chat').addEventListener('click',()=>{if(state.current)window.location.href='/api/sessions/'+state.current.id+'/export';});
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
  state.me=await api('/api/me');
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
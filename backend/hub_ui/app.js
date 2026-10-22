'use strict';
function requestId(){
 const bytes=crypto.getRandomValues(new Uint8Array(16));
 bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
 const h=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
 return h.slice(0,8)+'-'+h.slice(8,12)+'-'+h.slice(12,16)+'-'+h.slice(16,20)+'-'+h.slice(20);
}
const $=id=>document.getElementById(id);
const client=localStorage.getItem('hub-client')||'browser-'+requestId();localStorage.setItem('hub-client',client);
let loginRequested=false;
let state=null,view='chat',fingerprint='',draftId=null,refreshing=false,connectionError=false;
let incidentItems=[],incidentFingerprint=null;
const recalls=new Map();
let memoryItems=[],memoryCursor=null,memoryContext=null,memoryDirty=true,loadingMemory=false;
const names={auto:'Автоматически · помощник / кодинг',mausbot:'MausBot',ollama:'Ollama',codex:'Codex',hermes:'Hermes',orchestrator:'Codex · Orchestrator',openai:'OpenAI API',anthropic:'Anthropic API'};
const statusNames={queued:'В очереди',running:'Думает',tool:'Вызывает инструмент',completed:'Готово',failed:'Ошибка',cancelled:'Остановлено',unknown:'Результат неизвестен',needs_user:'Нужен ответ в MausBot'};
const actionNames={queued:'В очереди',running:'Узел выполняет',succeeded:'Узел подтвердил',failed:'Ошибка',cancelled:'Отменено',unknown:'Результат неизвестен',fired:'Завершён'};
const terminal=new Set(['completed','failed','cancelled','unknown','needs_user']);
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
function mausbotLink(task){const chat=state.chats?.find(c=>c.adapter==='mausbot'&&c.native_id===task.native_id);if(!chat)return null;try{const url=new URL(chat.host);if(!['http:','https:'].includes(url.protocol)||url.username||url.password)return null;const a=el('a','Открыть MausBot');a.href=url.origin;a.target='_blank';a.rel='noopener noreferrer';return a;}catch{return null;}}
function error(message){$('error').hidden=!message;$('error').textContent=message||'';}
async function api(path,body){const r=await fetch(path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});const value=await r.json();if(!r.ok){const e=new Error(value.error||'Запрос не выполнен');e.status=r.status;throw e;}return value;}
function showView(next){const changed=view!==next;if(changed&&view==='connections')$('connection-token').value='';view=next;for(const v of ['chat','memory','audio-notes','speech','agents','devices','notifications','access','connections','updates'])$(v+'-view').hidden=v!==next;document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===next));const labels={speech:'Запись для обучения','audio-notes':'Аудиозаметки',chat:'Разговор',memory:'Память',agents:'Агенты и задачи',notifications:'Уведомления',devices:'Узлы и действия',access:'Доступ',connections:'Подключения',updates:'Обновления'};$('section-label').textContent=labels[next].toUpperCase();$('title').textContent=next==='chat'?state?.contexts.find(c=>c.id===state.focus.context_id)?.title||'Разговор':labels[next];if(changed&&next==='updates'&&state)safe(loadUpdates);if(changed&&next==='connections'&&state)safe(loadConnections);if(changed&&next==='access'&&state)safe(loadAccess);if(changed&&next==='speech'&&state)safe(loadSpeechCapture);if(changed&&next==='audio-notes'&&state)safe(()=>loadAudioNotes(true));if(changed&&next==='notifications'&&state)safe(loadIncidents);if(next==='memory'&&state&&(memoryDirty||memoryContext!==state.focus.context_id)&&!loadingMemory)safe(()=>loadMemory(true));}
async function loadMemory(reset=false){if(loadingMemory||!state)return;loadingMemory=true;const context=state.focus.context_id;if(reset){memoryContext=context;memoryItems=[];memoryCursor=null;memoryDirty=false;}renderMemory();try{let path='/api/memory?context='+encodeURIComponent(context);if(memoryCursor&&!reset)path+='&after_scope='+encodeURIComponent(memoryCursor[0])+'&after_key='+encodeURIComponent(memoryCursor[1]);const page=await api(path);if(state.focus.context_id!==context)return;memoryItems.push(...page.items);memoryCursor=page.next;}finally{loadingMemory=false;renderMemory();}}
function renderMemory(){const items=memoryContext===state?.focus.context_id?memoryItems:[];$('memory-list').replaceChildren(...items.map(f=>{const d=el('article',undefined,'card');d.append(el('h3',f.key),el('p',f.body),el('p',(f.scope==='global'?'Общая память':'Текущая тема')+' · версия '+f.revision,'meta'));const a=el('div',undefined,'actions'),b=el('button','Удалить');b.onclick=()=>safe(async()=>{await api('/api/memory',{key:f.key,body:'',revision:f.revision,context_id:f.scope==='global'?null:f.scope,deleted:true});await loadMemory(true);await refresh();});a.append(b);d.append(a);return d;}));if(!items.length)$('memory-list').append(el('p',loadingMemory?'Загрузка знаний…':'Пока нет сохранённых знаний.','muted'));$('memory-more').hidden=!memoryCursor||memoryContext!==state?.focus.context_id;$('memory-more').disabled=loadingMemory;}
async function refresh(){if(refreshing||loginRequested)return;refreshing=true;try{const value=await api('/api/state?client='+encodeURIComponent(client));$('login').hidden=true;$('shell').hidden=false;if(connectionError){error('');connectionError=false;}state=value;if(view==='notifications')incidentItems=await api('/api/incidents');if(view==='speech')await loadSpeechCapture();render();}catch(e){if(e.status===401){loginRequested=true;state=null;$('login').hidden=false;$('shell').hidden=true;}else{connectionError=true;error('Нет связи с Hub. '+e.message);}}finally{refreshing=false;}}
function render(){
 const context=state.focus.context_id,previous=localStorage.getItem('hub-provider')||$('provider').value||(state.providers.auto?'auto':'ollama');
 const recall=recalls.get(context);$('recall-status').hidden=!recall;$('recall-status').textContent=recall?'Поиск по знаниям: '+recall.selected+' найдено · '+recall.elapsedMs+' мс':'';
 const options=Object.entries(state.providers);if(JSON.stringify(options)!==$('provider').dataset.state){$('provider').replaceChildren(...options.map(([k,v])=>{const o=el('option',(names[k]||k)+(v.ready?'':' · недоступен'));o.value=k;o.disabled=!v.ready;return o;}));$('provider').dataset.state=JSON.stringify(options);}
 if(options.some(([k,v])=>k===previous&&v.ready))$('provider').value=previous;
 $('topics').replaceChildren(...state.contexts.map(c=>{const b=el('button',c.title,'topic'+(c.id===context?' active':''));b.onclick=()=>choose(c.id);return b;}));
 showView(view);
 const active=state.tasks.filter(t=>t.context_id===context&&!terminal.has(t.status));
 $('working').hidden=!active.length;$('working-text').textContent=active.length?(statusNames[active[0].status]+' · '+(names[active[0].provider]||active[0].provider)):'';$('state-label').textContent=active.length?'В работе':'На связи';$('cancel').onclick=()=>safe(async()=>{await api('/api/cancel',{id:active[0].id});await refresh();});
 const nextFingerprint=JSON.stringify([context,state.history,active.map(t=>[t.id,t.output,t.status])]);
 if(nextFingerprint!==fingerprint){const m=$('messages'),near=m.scrollHeight-m.scrollTop-m.clientHeight<100;fingerprint=nextFingerprint;if(state.history.length||active.length){m.replaceChildren();for(const entry of state.history){if(entry.role==='tool'){const d=el('details',undefined,'bubble tool');let data;try{data=JSON.parse(entry.body);}catch{data={name:'Результат инструмента',result:entry.body};}d.append(el('summary',data.name),el('pre',JSON.stringify(data,null,2)));m.append(d);}else{const b=el('div',undefined,'bubble '+(entry.role==='user'?'user':'assistant'));b.append(el('span',entry.role==='user'?'Вы':names[entry.source]||entry.source,'byline'),el('div',entry.body));m.append(b);}}for(const t of active){if(t.output){const b=el('div',undefined,'bubble assistant');b.append(el('span',(names[t.provider]||t.provider)+' · отвечает','byline'),el('div',t.output));m.append(b);}}}else if(!m.querySelector('.empty')){const d=el('div',undefined,'empty');d.append(el('p','ВАШ АССИСТЕНТ','eyebrow'),el('h2','О чём поговорим?'),el('p','Начните разговор в этой теме.'));m.replaceChildren(d);}if(near)m.scrollTop=m.scrollHeight;}
 $('memory-preview').replaceChildren(...state.memory.slice(0,3).map(f=>{const d=el('div',undefined,'fact');d.append(el('h3',f.key),el('p',f.body.length>160?f.body.slice(0,160)+'…':f.body));return d;}));if(!state.memory.length)$('memory-preview').append(el('p','Пока нет сохранённых знаний. Их можно добавить в разделе «Память».','muted'));
 $('connections').replaceChildren(...options.map(([k,v])=>{const d=el('div',undefined,'service'),name=el('div',names[k]||k);name.append(el('small',v.detail));d.append(name,el('span',v.ready?'Готов':'Нет связи','service-status'+(v.ready?'':' off')));return d;}));
 $('node-list').replaceChildren(...(state.nodes||[]).map(n=>{const d=el('article',undefined,'card');d.append(el('h3',n.label),el('p',n.id+' · '+(n.online?'На связи':'Нет связи'),'meta'),el('p',n.capabilities.join(' · ')));return d;}));
 $('timer-list').replaceChildren(...(state.timers||[]).map(t=>{const d=el('article',undefined,'card');d.append(el('h3',t.label),el('p',t.status==='running'?'Осталось '+t.remaining_seconds+' сек.':actionNames[t.status]||t.status,'meta'));if(t.status==='running'){const b=el('button','Отменить');b.onclick=()=>safe(async()=>{await api('/api/capabilities/call',{id:requestId(),context_id:state.focus.context_id,client,node:'hub',capability:'timer.cancel',arguments:{timer_id:t.id}});await refresh();});d.append(b);}return d;}));
 if(!state.timers?.length)$('timer-list').append(el('p','Таймеров в этой теме пока нет.','muted'));
 $('call-list').replaceChildren(...(state.calls||[]).map(c=>{const d=el('article',undefined,'card');d.append(el('h3',c.node_id+' · '+c.capability),el('p',actionNames[c.status]||c.status,'meta'));if(c.error)d.append(el('p',c.error));return d;}));
 renderIncidents();
 $('notice-count').textContent=state.notifications.length;
 $('agent-cards').replaceChildren(...options.map(([k,v])=>{const d=el('article',undefined,'card');d.append(el('h3',names[k]||k),el('p',v.detail),el('p',v.ready?'Готов к запросам':'Подключение пока недоступно','meta'));return d;}));
 $('task-list').replaceChildren(...state.tasks.map(t=>{const d=el('article',undefined,'card');d.append(el('h3',t.prompt.slice(0,160)),el('p',(names[t.provider]||t.provider)+' · '+statusNames[t.status],'meta'));if(t.output||t.error)d.append(el('p',(t.error||t.output).slice(0,3000)));const external=mausbotLink(t);if(external)d.append(external);if(!terminal.has(t.status)||t.status==='needs_user'){const b=el('button','Остановить');b.onclick=()=>safe(async()=>{await api('/api/cancel',{id:t.id});await refresh();});d.append(b);}return d;}));if(!state.tasks.length)$('task-list').append(el('p','Задач пока нет. Выберите агента и отправьте запрос.','muted'));
 $('chat-list').replaceChildren(...state.chats.map(c=>{
  const d=el('article',undefined,'card');d.append(el('h3',c.title||'Чат агента'),el('p',c.adapter+' · '+c.host,'meta'));
  if(c.adapter==='codex'){
   const actions=el('div',undefined,'actions'),report=el('button','Отчёт'),select=el('button','Выбрать для разговора');
   report.onclick=()=>safe(async()=>{const r=await api('/api/codex/chats/'+encodeURIComponent(c.id));$('codex-report-title').textContent=r.title;$('codex-report-body').textContent=(statusNames[r.status]||r.status)+(r.stale?' · сохранённый статус':'')+(r.read_error?' · чтение временно недоступно':'')+'\n\n'+(r.report||r.error||'Итогового отчёта пока нет.')+(r.files.length?'\n\nИзменённые файлы:\n'+r.files.join('\n'):'');$('codex-report-dialog').showModal();});
   select.onclick=()=>safe(async()=>{const r=await api('/api/codex/voice',{id:requestId(),client,context_id:state.focus.context_id,text:'выбери задачу кодекса '+c.id});showView('chat');$('codex-report-title').textContent=c.title;$('codex-report-body').textContent=r.reply;$('codex-report-dialog').showModal();});
   actions.append(report,select);d.append(actions);
  }
  return d;
 }));
 $('notification-list').replaceChildren(...[...state.notifications].reverse().map(n=>{const d=el('article',undefined,'card');d.append(el('h3',state.contexts.find(c=>c.id===n.context_id)?.title||'Тема'),el('p',n.body),el('p',(names[n.source]||n.source)+' · '+new Date(n.created_at*1000).toLocaleString('ru'),'meta'));const b=el('button','Обсудить');b.onclick=()=>safe(async()=>{await api('/api/follow',{client,notification:n.id,revision:state.focus.revision});showView('chat');await refresh();});d.append(b);return d;}));if(!state.notifications.length)$('notification-list').append(el('p','Уведомления появятся после завершения задач.','muted'));
}
const deliveryNames={queued:'Ожидает подключения',queued_on_phone:'В очереди телефона',running:'Передаётся',submitted:'Передано приложению',blocked:'Уведомления запрещены',failed:'Ошибка',unknown:'Подтверждения нет'};
function renderIncidents(){
 const next=JSON.stringify(incidentItems);if(incidentFingerprint===next)return;incidentFingerprint=next;
 $('incident-list').replaceChildren(...incidentItems.map(n=>{
  const d=el('article',undefined,'card');d.append(el('h3',n.title),el('p',n.source+' · '+(statusNames[n.status]||n.status),'meta'));
  if(n.assessment)d.append(el('p',n.assessment.summary));if(n.error)d.append(el('p',n.error));
  if(!n.routed)d.append(el('p','Ожидает разбора или регистрации получателя','meta'));
  for(const r of n.deliveries)d.append(el('p',(r.channel==='desktop'?'Coucou':'Телефон')+' · '+(deliveryNames[r.status]||r.status),'meta'));
  const details=el('details'),summary=el('summary','Исходные данные'),raw=el('pre');details.append(summary,raw);
  details.ontoggle=()=>{if(details.open&&!raw.textContent)safe(async()=>{const full=await api('/api/incidents/'+encodeURIComponent(n.id));raw.textContent=full.payload;});};d.append(details);return d;
 }));if(!incidentItems.length)$('incident-list').append(el('p','Вебхуки пока не поступали.','muted'));
}
async function loadIncidents(){incidentItems=await api('/api/incidents');renderIncidents();}
async function safe(fn){try{error('');await fn();}catch(e){error(e.message);}}
async function choose(context){await safe(async()=>{await api('/api/focus',{client,context_id:context,revision:state.focus.revision});draftId=null;fingerprint='';await refresh();});}
document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>showView(b.dataset.view));$('show-memory').onclick=()=>showView('memory');
$('memory-more').onclick=()=>safe(()=>loadMemory());
$('login-form').onsubmit=async e=>{e.preventDefault();try{await api('/api/login',{username:$('username').value,password:$('password').value});$('password').value='';$('login-error').textContent='';loginRequested=false;await refresh();if(view==='access')await loadAccess();if(view==='connections')await loadConnections();if(view==='updates')await loadUpdates();}catch(ex){$('login-error').textContent=ex.message;}};
$('message-form').onsubmit=e=>{e.preventDefault();safe(async()=>{const prompt=$('prompt').value.trim();if(!prompt)return;$('send').disabled=true;try{draftId ||=requestId();await api('/api/messages',{id:draftId,context_id:state.focus.context_id,client,provider:$('provider').value,prompt});draftId=null;$('prompt').value='';await refresh();}finally{$('send').disabled=false;$('prompt').focus();}});};
$('prompt').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();$('message-form').requestSubmit();}};
$('prompt').oninput=()=>{draftId=null;};$('provider').onchange=()=>{draftId=null;localStorage.setItem('hub-provider',$('provider').value);};
$('new-topic').onclick=()=>{$('topic-dialog').showModal();$('topic-name').focus();};$('topic-close').onclick=()=>$('topic-dialog').close();
$('topic-form').onsubmit=e=>{e.preventDefault();safe(async()=>{const c=await api('/api/contexts',{title:$('topic-name').value});$('topic-dialog').close();$('topic-name').value='';await choose(c.id);showView('chat');});};
$('memory-form').onsubmit=e=>{e.preventDefault();safe(async()=>{const scope=$('memory-scope').value==='global'?null:state.focus.context_id,key=$('memory-key').value;const current=[...memoryItems,...state.memory].find(m=>m.key===key&&m.scope===(scope||'global'));await api('/api/memory',{key,body:$('memory-body').value,revision:current?.revision||0,context_id:scope});$('memory-key').value='';$('memory-body').value='';await loadMemory(true);await refresh();});};
const theme=localStorage.getItem('hub-theme');if(theme==='light')document.body.classList.add('light');$('theme').onclick=()=>{document.body.classList.toggle('light');localStorage.setItem('hub-theme',document.body.classList.contains('light')?'light':'dark');};
let after=0;async function events(){if(!state||loginRequested)return;try{const items=await api('/api/events?after='+after);if(items.length){after=items.at(-1).seq;for(const e of items){if(e.kind==='memory.retrieved')recalls.set(e.context_id,e.data);if(e.kind==='memory.changed'&&(e.data.scope==='global'||e.data.scope===state.focus.context_id))memoryDirty=true;}const tools=items.filter(e=>e.context_id===state.focus.context_id&&['tool.started','tool.succeeded','agent.tool','memory.retrieved','task.routed'].includes(e.kind));if(tools.length){for(const e of tools.slice(-5)){const d=el('div',undefined,'action');if(e.kind==='task.routed'){d.append(el('span','Выбран агент: '+(names[e.data.target]||e.data.target)),el('small',e.data.route==='coding'?'Работа с кодом':'Обычный помощник'));}else if(e.kind==='memory.retrieved'){d.append(el('span','Поиск по знаниям: '+e.data.selected),el('small',e.data.elapsedMs+' мс · '+e.data.memoryBytes+' байт'));}else{d.append(el('span',e.data.tool||e.data.name||'Инструмент'),el('small',e.kind==='tool.started'?'Вызов':'Результат получен'));}$('activity').prepend(d);}while($('activity').children.length>6)$('activity').lastChild.remove();}}}catch{}}
refresh();setInterval(refresh,1000);setInterval(events,1200);

$('timer-form').onsubmit=e=>{e.preventDefault();safe(async()=>{await api('/api/capabilities/call',{id:requestId(),context_id:state.focus.context_id,client,node:'hub',capability:'timer.create',arguments:{duration_seconds:Number($('timer-duration').value),label:$('timer-label').value.trim()||'Таймер'}});await refresh();});};

let audioNotes=[],audioOffset=null,audioLoading=false;
async function loadAudioNotes(reset=false){
 if(audioLoading)return;audioLoading=true;
 try{const offset=reset?0:audioOffset;if(offset===null)return;
 const page=await api('/api/audio-notes?query='+encodeURIComponent($('audio-query').value)+'&offset='+offset);
 if(reset)audioNotes=[];audioNotes.push(...page.items);audioOffset=page.next;
 $('audio-notes-list').replaceChildren(...audioNotes.map(n=>{
  const d=el('article',undefined,'card');d.append(el('h3',n.title),el('p',new Date(n.createdAt).toLocaleString('ru')+' · '+Math.round(n.durationMs/1000)+' с','meta'));
  d.append(el('p',n.text||(n.status==='ready'?'Речь не найдена':'Текст ещё не распознан')));
  const audio=el('audio');audio.controls=true;audio.preload='none';audio.src='/api/audio-notes/audio/'+encodeURIComponent(n.audioSha256);d.append(audio);return d;
 }));if(!audioNotes.length)$('audio-notes-list').append(el('p','Аудиозаметок пока нет. Запишите первую в Android-ассистенте.','muted'));
 $('audio-notes-more').hidden=audioOffset===null;
 }finally{audioLoading=false;}
}
$('audio-search-form').onsubmit=e=>{e.preventDefault();safe(()=>loadAudioNotes(true));};
$('audio-notes-more').onclick=()=>safe(()=>loadAudioNotes());

async function loadSpeechCapture(){
 const [s,corpus,pipeline]=await Promise.all([api('/api/speech-capture'),api('/api/speech-samples'),api('/api/speech-pipeline')]);
 $('speech-status').textContent=(s.phase==='recording'?'Запись идёт · '+s.durationSeconds+' с · частей '+s.chunks:s.phase==='starting'?'Ожидаю запуска / разрешений macOS':s.phase==='stopping'?'Сохраняю последние части…':'Запись остановлена')+(s.error?' · '+s.error:'');
 $('speech-start').disabled=s.alive||!s.available;$('speech-stop').disabled=!s.alive;
 $('speech-mic').disabled=s.alive;$('speech-system').disabled=s.alive;$('speech-live').disabled=s.alive;
 if(s.transcriptPreview)$('speech-status').textContent+=' · '+s.transcriptPreview;
 $('speech-teacher').disabled=pipeline.running||!pipeline.teacherAvailable||!corpus.total;
 $('speech-train').disabled=pipeline.running||!pipeline.trainingAvailable||!pipeline.teacherReport?.readyToTrain;
 $('speech-pipeline-status').textContent=(pipeline.running?(pipeline.action==='teacher'?'Размечаю записи…':'Дообучаю T-one…'):pipeline.phase==='completed'?(pipeline.action==='train'?'Дообучение завершено. Веса сохранены на компьютере; перед установкой в Android нужны проверка и экспорт.':'Разметка завершена'):pipeline.phase==='failed'?'Ошибка: '+pipeline.error:'Разметка ещё не запускалась')+(pipeline.teacherReport?' · подготовлено фраз: '+pipeline.teacherReport.clips+(!pipeline.teacherReport.readyToTrain?' · для обучения нужна ещё одна отдельная сессия записи':''):'');
 $('speech-corpus').textContent='Записей: '+corpus.total+' · с подтверждённым человеком текстом: '+corpus.reviewed;
 if(!s.available)$('speech-status').textContent='Локальный аудиозахват недоступен. Нужен macOS helper.';
}
$('speech-start').onclick=()=>safe(async()=>{const sources=[];if($('speech-mic').checked)sources.push('mic');if($('speech-system').checked)sources.push('system');if(!sources.length)throw new Error('Выберите источник звука');await api('/api/speech-capture',{action:'start',sources,transcribe:$('speech-live').checked});await loadSpeechCapture();});
$('speech-stop').onclick=()=>safe(async()=>{await api('/api/speech-capture',{action:'stop'});await loadSpeechCapture();});

$('speech-teacher').onclick=()=>safe(async()=>{await api('/api/speech-pipeline',{action:'teacher'});await loadSpeechCapture();});
$('speech-train').onclick=()=>safe(async()=>{await api('/api/speech-pipeline',{action:'train'});await loadSpeechCapture();});

// Owner account and per-application credentials. Never persist a password or key in web storage.
$('token-login-form').onsubmit=async e=>{e.preventDefault();try{await api('/api/login',{token:$('token').value});$('token').value='';$('login-error').textContent='';loginRequested=false;await refresh();showView('access');}catch(ex){$('login-error').textContent=ex.message;}};
function accountLogin(){ $('connection-token').value='';loginRequested=true;clearNewKey();for(const id of ['new-password','repeat-password','current-password'])$(id).value='';$('shell').hidden=true;$('login').hidden=false;$('username').focus(); }
$('account-login').onclick=accountLogin;
$('logout').onclick=()=>safe(async()=>{await api('/api/logout',{});state=null;clearNewKey();accountLogin();});
let accountConfigured=false;
async function loadAccess(){
 clearNewKey();
 try{
  const account=await api('/api/account');accountConfigured=account.configured;
  $('account-login').hidden=true;$('account-settings').hidden=false;$('account-settings').open=!account.configured;$('account-form').hidden=false;$('key-management').hidden=!account.configured;
  $('account-heading').textContent=account.configured?'Логин и пароль владельца':'Создайте учётную запись владельца';
  $('account-username').value=account.username||'';$('current-password-field').hidden=!account.configured;$('current-password').required=account.configured;
  $('access-status').textContent=account.configured?'Выполнен вход: '+account.username+'. Панель доступна по логину и паролю.':'Задайте логин и пароль для следующих входов. Существующие приложения продолжат работать.';
  if(account.configured)await loadKeys();
 }catch(e){if(e.status!==403)throw e;$('account-settings').hidden=true;$('account-form').hidden=true;$('key-management').hidden=true;$('account-login').hidden=false;$('access-status').textContent='Управление доступом доступно владельцу после входа по логину и паролю.';}
}
function clearNewKey(){ $('new-key').value='';$('new-key-card').hidden=true;$('copy-key-status').textContent=''; }
async function loadKeys(){
 const keys=await api('/api/access-keys');$('access-keys').replaceChildren();
 const scopes={app:'Данные хаба и ассистент',webhook:'Только события',mcp:'Только MCP',owner:'Полный доступ · старый ключ',deploy:'Только загрузка релизов · без установки'};
 if(!keys.length)$('access-keys').append(el('p','Пока нет ключей приложений.','muted'));
 for(const key of keys){
  const card=el('div',undefined,'card');card.append(el('h4',key.name),el('p',scopes[key.scope]||key.scope,'muted'),el('p','Создан: '+new Date(key.created_at*1000).toLocaleString()));
  if(key.revoked_at)card.append(el('p','Доступ отозван · '+new Date(key.revoked_at*1000).toLocaleString(),'muted'));
  else {const button=el('button','Отозвать доступ');button.onclick=()=>safe(async()=>{await api('/api/access-keys/revoke',{id:key.id});clearNewKey();await loadKeys();});card.append(button);}
  $('access-keys').append(card);
 }
}
$('account-form').onsubmit=e=>{e.preventDefault();safe(async()=>{
 if($('new-password').value!==$('repeat-password').value)throw new Error('Пароли не совпадают');
 $('save-account').disabled=true;
 try{await api('/api/account',{username:$('account-username').value,password:$('new-password').value,current_password:$('current-password').value});
 for(const id of ['new-password','repeat-password','current-password'])$(id).value='';await loadAccess();$('access-status').textContent='Логин и пароль сохранены. Следующий вход — по ним.';
 }finally{$('save-account').disabled=false;}
});};
$('key-form').onsubmit=e=>{e.preventDefault();safe(async()=>{const result=await api('/api/access-keys',{name:$('key-name').value,scope:$('key-scope').value});$('key-name').value='';await loadKeys();$('new-key').value=result.token;$('new-key-card').hidden=false;$('new-key').focus();$('new-key').select();});};
$('copy-key').onclick=()=>safe(async()=>{await navigator.clipboard.writeText($('new-key').value);$('copy-key-status').textContent='Ключ скопирован';});
$('hide-key').onclick=clearNewKey;

api('/api/auth/status').then(s=>{$('legacy-login').hidden=s.configured;}).catch(()=>{});

// Connection secrets stay in the submitted form only; GET never returns a token.
let connectionSettings=null,editingConnection=null;
$('connections-login').onclick=accountLogin;
function newConnection(){editingConnection=null;$('connection-heading').textContent='Добавить сервер';for(const id of ['connection-name','connection-url','connection-token'])$(id).value='';$('connection-model').value='hermes-agent';$('connection-token').required=true;$('connection-result').textContent='';}
$('connection-new').onclick=newConnection;
function connectionBody(){return {id:editingConnection,revision:connectionSettings.revision,name:$('connection-name').value,url:$('connection-url').value,model:$('connection-model').value,token:$('connection-token').value};}
async function loadConnections(){
 try{connectionSettings=await api('/api/connections');$('connections-owner').hidden=false;$('connections-login').hidden=true;renderConnections();}
 catch(e){if(e.status!==403)throw e;$('connections-owner').hidden=true;$('connections-login').hidden=false;$('connections-status').textContent='Для настройки серверов войдите по логину и паролю владельца.';}
}
function renderConnections(){
 const settings=connectionSettings;$('connections-list').replaceChildren();
 const active=settings.profiles.find(p=>p.id===settings.active);
 $('connections-status').textContent=active?'Для новых вопросов используется: '+active.name:settings.legacy_available?'Используется исходное подключение сервера.':'Сервер ещё не выбран.';
 for(const profile of settings.profiles){const card=el('article',undefined,'card');card.append(el('h3',profile.name+(profile.id===settings.active?' · Используется':'')),el('p',profile.url,'muted'),el('p','Модель: '+profile.model));
  if(profile.checked_at)card.append(el('p','Связь проверена: '+new Date(profile.checked_at*1000).toLocaleString(),'hint'));
  const actions=el('div',undefined,'actions'),edit=el('button','Настроить'),activate=el('button','Использовать');
  edit.onclick=()=>{editingConnection=profile.id;$('connection-heading').textContent='Настроить сервер';$('connection-name').value=profile.name;$('connection-url').value=profile.url;$('connection-model').value=profile.model;$('connection-token').value='';$('connection-token').required=false;$('connection-result').textContent='Оставьте ключ пустым, чтобы сохранить прежний. Для нового адреса нужен новый ключ.';$('connection-name').focus();};
  activate.disabled=profile.id===settings.active;
  activate.onclick=()=>safe(async()=>{activate.disabled=true;try{connectionSettings=await api('/api/connections/activate',{id:profile.id,revision:connectionSettings.revision});renderConnections();await refresh();}finally{activate.disabled=false;}});
  actions.append(edit,activate);card.append(actions);$('connections-list').append(card);
 }
 if(settings.legacy_available&&settings.active){const button=el('button','Вернуться к исходному подключению');button.onclick=()=>safe(async()=>{connectionSettings=await api('/api/connections/activate',{id:null,revision:connectionSettings.revision});renderConnections();await refresh();});$('connections-list').append(button);}
}
$('connection-test').onclick=()=>safe(async()=>{const button=$('connection-test');button.disabled=true;try{const result=await api('/api/connections/test',connectionBody());$('connection-result').textContent=result.check.detail;}finally{button.disabled=false;}});
$('connection-form').onsubmit=e=>{e.preventDefault();safe(async()=>{const button=$('connection-save');button.disabled=true;try{const result=await api('/api/connections/save',connectionBody());connectionSettings=result.settings;editingConnection=result.connection.id;renderConnections();$('connection-result').textContent=connectionSettings.active===editingConnection?'Настройки действующего сервера сохранены. Они применятся к новым вопросам.':'Подключение сохранено. Нажмите «Использовать», чтобы переключить ассистента.';$('connection-token').required=false;}finally{button.disabled=false;$('connection-token').value='';}});};

async function loadUpdates(){
 try{const result=await api('/api/releases');renderUpdates(result);}
 catch(e){if(e.status!==403)throw e;$('updates-owner').hidden=true;$('updates-status').textContent='Для обновления хаба войдите по логину и паролю владельца.';}
}
function renderUpdates(result){
 $('updates-owner').hidden=!result.enabled;$('release-list').replaceChildren();
 const phases={running:'Работает',queued:'Подготовка к переключению',trial:'Проверка запуска', 'rolled-back':'Возвращена предыдущая версия','recovery-needed':'Нужно восстановление'};
 $('updates-status').textContent=result.enabled?'Версия: '+(result.active||'—')+'. '+(phases[result.phase]||result.phase)+(result.error?' · '+result.error:''):result.detail||'Служба обновлений ещё не настроена.';
 for(const release of result.releases||[]){const card=el('article',undefined,'card');card.append(el('h3',release.version+(release.version===result.active?' · Используется':'')));
  const button=el('button',release.version===result.previous?'Вернуть эту версию':'Установить');button.disabled=release.version===result.active||!['running','rolled-back'].includes(result.phase);
  button.onclick=()=>safe(async()=>{button.disabled=true;try{const status=await api('/api/releases/activate',{version:release.version});renderUpdates(status);$('updates-status').textContent='Переключение запрошено. Дождитесь запуска и войдите в Hub снова.';}catch(e){button.disabled=false;throw e;}});
  card.append(button);$('release-list').append(card);
 }
}
$('updates-refresh').onclick=()=>safe(loadUpdates);
$('release-form').onsubmit=e=>{e.preventDefault();safe(async()=>{const file=$('release-file').files[0];if(!file||file.size>4194304)throw new Error('Выберите пакет до 4 МиБ');$('release-upload').disabled=true;
 try{const bytes=await file.arrayBuffer();const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),b=>b.toString(16).padStart(2,'0')).join('');const response=await fetch('/api/releases/stage',{method:'POST',headers:{'Content-Type':'application/gzip','X-Release-SHA256':digest},body:bytes});const result=await response.json();if(!response.ok)throw new Error(result.error||'Не удалось загрузить релиз');renderUpdates(result);$('release-file').value='';}
 finally{$('release-upload').disabled=false;}
});};

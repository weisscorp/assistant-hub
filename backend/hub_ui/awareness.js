'use strict';
function requestId(){
 const bytes=crypto.getRandomValues(new Uint8Array(16));
 bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
 const h=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
 return h.slice(0,8)+'-'+h.slice(8,12)+'-'+h.slice(12,16)+'-'+h.slice(16,20)+'-'+h.slice(20);
}
const $=id=>document.getElementById(id),names={slack:'Slack',telegram:'Telegram',codex:'Codex'},states={open:'Ждёт внимания',in_progress:'В работе',snoozed:'Отложено',closed:'Закрыто'};
let snapshot=null,busy=false;
const session=requestId();
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
async function api(path,body){const r=await fetch(path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});const v=await r.json();if(!r.ok){if(r.status===401)$('login').hidden=false;throw Error(v.error||'Запрос не выполнен');}return v;}
async function run(fn){if(busy)return;busy=true;try{$('feedback').textContent='';await fn();}catch(e){$('feedback').textContent=e.message;}finally{busy=false;}}
async function refresh(){snapshot=await api('/api/awareness');$('login').hidden=true;render();}
function render(){
 $('sources').replaceChildren(...snapshot.sources.map(s=>{const n=el('article',undefined,'source');n.append(el('h3',names[s.source]),el('p',s.last_received?'Последнее событие: '+new Date(s.last_received*1000).toLocaleString('ru'):'Событий пока нет'),el('p','Постоянное подключение не настроено'));return n;}));
 $('routes').replaceChildren(...(snapshot.routes||[]).map(r=>el('p',r.label+' → '+r.owner+' · '+Object.entries(r.context_refs).map(([k,v])=>k+': '+v).join(' · '))));
 if(!snapshot.routes?.length)$('routes').append(el('p','Маршруты ещё не настроены.'));
 const filter=$('filter').value,cases=snapshot.cases.filter(c=>filter==='all'||(filter==='closed'?c.effective_status==='closed':!['closed','snoozed'].includes(c.effective_status)));
 $('cases').replaceChildren(...cases.map(c=>{
  const card=el('article',undefined,'card'),tags=el('div');tags.append(el('span',c.project,'badge'),el('span',states[c.effective_status],c.priority==='important'?'badge important':'badge'));card.append(tags,el('h2',c.title),el('p',c.reason+' · Событий: '+c.events.length,'meta'));
  const details=el('details'),summary=el('summary','Источники и история решений');details.append(summary);
  for(const e of c.events){const n=el('div',undefined,'event');n.append(el('h3',names[e.source]+' · '+e.title),el('p',e.body));if(e.url){const a=el('a','Открыть источник');a.href=e.url;a.target='_blank';a.rel='noopener noreferrer';n.append(a);}details.append(n);}
  for(const d of c.decisions)details.append(el('p',d.action+' · '+new Date(d.created_at*1000).toLocaleString('ru'),'meta'));card.append(details);
  if(c.status==='snoozed')card.append(el('p','Отложено до '+new Date(c.snoozed_until*1000).toLocaleString('ru'),'meta'));
  const actions=el('div',undefined,'actions');
  for(const [action,label] of c.effective_status==='closed'?[['reopen','Открыть снова']]:[['take','Взять в работу'],['snooze','Через час'],['close','Закрыть']]){const b=el('button',label,action==='take'?'primary':'');b.onclick=()=>run(async()=>{await api('/api/awareness/decision',{id:c.id,revision:c.revision,action});await refresh();});actions.append(b);}
  if(snapshot.devices.length){const select=el('select');select.setAttribute('aria-label','Телефон для уведомления');for(const d of snapshot.devices){const o=el('option',d.device);o.value=d.device;select.append(o);}const send=el('button','В inbox телефона');send.onclick=()=>run(async()=>{const r=await api('/api/awareness/publish',{id:c.id,revision:c.revision,device:select.value});await refresh();$('feedback').textContent=r.note;});actions.append(select,send);}else card.append(el('p','Для доставки в Android нужен сопряжённый телефон.','meta'));
  for(const d of c.deliveries)card.append(el('p',d.status==='queued_on_phone'?'В очереди inbox телефона · прочтение не подтверждено':'Доставка ожидает повтора','meta'));
  card.append(actions);return card;
 }));if(!cases.length)$('cases').append(el('p','В этой группе пока нет ситуаций.','empty'));
}
$('filter').onchange=()=>{if(snapshot)render();};$('refresh').onclick=()=>run(refresh);
$('demo').onclick=()=>run(async()=>{await api('/api/awareness/demo',{});await refresh();});
$('login').onsubmit=e=>{e.preventDefault();run(async()=>{await api('/api/login',{token:$('token').value});$('token').value='';await refresh();});};run(refresh);
$('turn-form').onsubmit=e=>{e.preventDefault();run(async()=>{const r=await api('/conversation',{version:1,session,id:requestId(),text:$('message').value,history:[],capabilities:[]});$('reply').textContent=r.reply;});};

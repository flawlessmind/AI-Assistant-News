'use strict';
const state = {status:null,profiles:[],company:null,role:'cfo',feed:[],feedAsOf:null,selected:null,run:null,version:0,busy:false,progressTimer:null,refreshing:false};
Object.assign(state,{searchRows:null,searchVersion:0,searchTimer:null,searchBusy:false,searchError:''});
Object.assign(state,{requests:new Map(),articleStates:new Map(),visibleRequestKey:null});
const $ = id => document.getElementById(id);
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}
function button(text, action, className='secondary') {
  const element = node('button', text, className); element.type='button';
  element.addEventListener('click',event=>{if(!element.disabled)return action(event);});return element;
}
function link(text,url) {
  const element=node('a',text);let parsed;
  try { parsed=new URL(url,location.href||location.origin); } catch {return element;}
  if (!['https:','http:'].includes(parsed.protocol)) return element;
  element.href=parsed.href; element.target='_blank'; element.rel='noopener noreferrer'; return element;
}
function dateText(value) {
  const date=value ? new Date(value) : null;
  return date && Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat('ru-RU',{dateStyle:'medium',timeZone:'Europe/Moscow'}).format(date) : 'Дата не установлена';
}
function notice(text, error=false) { $('notice').hidden=!text; $('notice').textContent=text; $('notice').className='notice'+(error?' error':''); }
async function request(path,body,retryAction=true) {
  const options=body ? {method:'POST',headers:{'Content-Type':'application/json','X-Alfa-Action':state.status.csrf_token},body:JSON.stringify(body)} : {};
  let response;
  try { response=await fetch(path,options); }
  catch { throw new Error('Нет соединения с сервером. Карточка сохранена; после восстановления соединения можно повторить действие.'+
    (body?.mode==='live'?' Сервер мог принять задание. Повтор проверит ту же попытку с сохранённым ID.':'')); }
  if (body && response.status===403 && retryAction) {
    // The server rejects this token before dispatch, so refreshing it cannot duplicate an LLM call.
    state.status=await request('/api/status');
    return request(path,body,false);
  }
  let data;
  try { data=await response.json(); }
  catch { throw new Error(`Сервер вернул некорректный ответ (HTTP ${response.status}). Результат не принят.`); }
  if (!response.ok) {const error=new Error(typeof data.error==='string'?data.error:'Не удалось выполнить запрос.');
    error.status=response.status;error.jobFailed=data.code==='previous_attempt_failed';throw error;}
  return data;
}
function stopProgress() {
  if (state.progressTimer!==null) clearInterval(state.progressTimer);
  state.progressTimer=null;
}
function updateActions() {
  $('detail').setAttribute('aria-busy',String(state.busy));
  for (const control of $('detail').querySelectorAll('[data-analysis-action]')) {
    control.disabled=(state.busy && control.dataset.allowConcurrent!=='true') || (control.dataset.requiresLlm==='true' && !state.status?.llm.live_enabled);
    if (!state.busy && control.dataset.idleLabel) control.textContent=control.dataset.idleLabel;
  }
  $('refresh').disabled=state.busy||state.refreshing;
  renderNavigation();
}
function focusOutcome(id='analysis-outcome') {
  const target=$(id); if (!target) return;
  target.focus({preventScroll:true});
  target.scrollIntoView({block:'start',behavior:'instant'});
}
function revealDetails(id) {
  const target=$(id); if (!target) return;
  target.open=true; focusOutcome(id);
}
function renderNavigation() {
  const navigation=$('analysis-navigation'); if (!navigation) return;
  const run=state.run;
  const saved=state.feed.find(row=>row.cached_run_id && (!run || row.event.event_id===run.event.event_id));
  const context=`${state.company} · ${state.role==='cfo'?'Финансовый директор':'Руководитель бизнеса'}`;
  const text=node('div');
  const actions=node('div',null,'analysis-nav-actions');
  const add=(label,action,id,primary=false)=>{
    const control=button(label,action,primary?'primary':'secondary');control.id=id;control.disabled=state.busy;actions.append(control);
  };
  if (run?.status==='complete' || !run && saved) {
    text.append(node('strong','Основной анализ готов'),node('p',context,'muted'));
    add('Открыть основной анализ',()=>run?focusOutcome():selectEvent(saved.event.event_id),'open-main-analysis',true);
    if (run) add('Что проверить сейчас',()=>focusOutcome('analysis-next-step'),'open-next-step');
  } else if (run?.status==='answered') {
    text.append(node('strong','Открыт ответ на уточняющий вопрос'),node('p',context,'muted'));
    add('Показать ответ',()=>focusOutcome(),'open-answer');
    add(saved?'Открыть основной анализ':'Вернуться к новости',()=>selectEvent(run.event.event_id),'open-main-analysis',true);
  } else if (run) {
    text.append(node('strong','Карточка выбранной новости'),node('p',context,'muted'));
    add('Открыть карточку новости',()=>focusOutcome(),'open-news-card',true);
  } else {
    text.append(node('strong','Начните с новости'),node('p','Нажмите карточку в блоке «События для вас». Здесь появится переход к результату.','muted'));
  }
  if (run?.bank_evidence.length) add('Источники',()=>revealDetails('bank-sources'),'open-bank-sources');
  navigation.replaceChildren(text,actions);
}
function startProgress(mode,version) {
  const feedback=$('analysis-feedback'); if (!feedback) return;
  const started=Date.now(); feedback.hidden=false; feedback.className='analysis-feedback working'; feedback.setAttribute('role','status');
  const title=node('strong',mode==='live'?'Готовим и проверяем инсайт…':'Ищем источники…');
  const elapsed=node('span','');
  feedback.replaceChildren(title,elapsed);
  const generate=$('generate-insight'); if (generate) generate.textContent=mode==='live'?'Готовим инсайт…':'Ищем источники…';
  const tick=()=>{
    if (version!==state.version) return;
    elapsed.textContent=`Прошло ${Math.floor((Date.now()-started)/1000)} сек. Результат появится в этой карточке.`;
  };
  tick(); state.progressTimer=setInterval(tick,1000);
  feedback.focus({preventScroll:true});
}
function showRequestError(error,retry) {
  let feedback=$('analysis-feedback');
  if (!feedback) {
    feedback=node('div'); feedback.id='analysis-feedback'; feedback.tabIndex=-1;
    $('detail').append(feedback);
  }
  feedback.hidden=false; feedback.className='analysis-feedback error'; feedback.setAttribute('role','alert');
  feedback.replaceChildren(node('strong','Не удалось завершить запрос'),node('p',error.message));
  if (retry) feedback.append(button('Повторить запрос',retry));
  updateActions(); focusOutcome('analysis-feedback');
}
function resultKey(eventId,context=state) { return `alfa.news.last-run.v1:${context.company_id||context.company}:${context.role}:${eventId}`; }
function attemptKey(eventId,context=state) { return `alfa.news.last-attempt.v1:${context.company_id||context.company}:${context.role}:${eventId}`; }
function articleKey(eventId,context=state) { return JSON.stringify([context.company_id||context.company,context.role,eventId,context.as_of||context.feedAsOf]); }
function isAnalysisAttempt(run) { return run.status==='complete'||run.llm_request_attempted===true||run.runtime_llm_used===true||run.regeneration_requested===true; }
function visibleContext(context) { return context.company_id===state.company && context.role===state.role && context.as_of===state.feedAsOf; }
function activeArticleRequest(eventId,context=state) {
  return state.requests.get(jobKey({...context,event_id:eventId}));
}
function articleStatus(context,status) {
  const key=articleKey(context.event_id,context),prior=state.articleStates.get(key);
  if([...state.requests.values()].some(entry=>entry.articleKey===key&&entry.live&&!entry.finished))status='working';
  state.articleStates.set(key,{status});
  if (prior?.status!==status && visibleContext(context)) renderFeed();
}
function rememberContext() {
  try {localStorage.setItem('alfa.news.context.v1',JSON.stringify({company_id:state.company,role:state.role}));} catch {}
}
function normalizedTask(question='',task) {
  if(task && task!=='auto') return task;
  const text=question.toLowerCase().replaceAll('ё','е');
  if(!text.trim())return 'insight';
  if(/не хватает|недостающ|пробел|каки[ех].*данн.*(?:нуж|треб)|чего.*не.*хват/.test(text))return 'missing_data';
  if(/(?:найди|найти|подбери|поищи|дополнительн).*(?:источник|аналитик|исследован)|найти источники/.test(text))return 'sources';
  return 'explain';
}
function jobContext(payload) {
  const task=normalizedTask(payload.question||'',payload.task);
  const context={company_id:payload.company_id||payload.company,role:payload.role,event_id:payload.event_id,
    as_of:payload.as_of||payload.feedAsOf,mode:payload.mode||'sources',task,question:payload.question||'',
    parent_run_id:task==='explain'?(payload.parent_run_id||null):null};
  if(payload.regenerate){context.regenerate=true;context.request_id=payload.request_id;}
  return context;
}
const PENDING_PREFIX='alfa.news.pending.v2:';
const REQUEST_PREFIX='alfa.news.request.v1:';
function jobKey(payload) {return PENDING_PREFIX+JSON.stringify(jobContext(payload));}
function requestKey(payload) {return REQUEST_PREFIX+JSON.stringify(jobContext(payload));}
function pendingRequestId(payload) {
  try {const value=localStorage.getItem(requestKey(payload));return /^[a-f0-9]{32}$/.test(value||'')?value:null;} catch{return null;}
}
function clearRequestId(payload) {
  try {const key=requestKey(payload);if(localStorage.getItem(key)===payload.request_id)localStorage.removeItem(key);} catch {}
}
function pendingJob(payload) {
  try {const value=localStorage.getItem(jobKey(payload));return /^[a-f0-9]{32}$/.test(value||'')?value:null;} catch{return null;}
}
function clearPendingJob(key,jobId) {
  try {if(localStorage.getItem(key)===jobId)localStorage.removeItem(key);} catch {}
}
function renderBackgroundTasks() {
  if(!state.selected)return;
  let section=$('article-background-tasks');
  if(!section){section=block('Фоновые задания этой статьи');section.id='article-background-tasks';$('detail').append(section);}
  section.replaceChildren();const entries=new Map();
  try {
    for(let index=0;index<localStorage.length;index++) {
      const key=localStorage.key(index);if(!key?.startsWith(PENDING_PREFIX))continue;
      const context=JSON.parse(key.slice(PENDING_PREFIX.length));
      if(pendingJob(context))entries.set(key,context);
    }
    for(let index=0;index<localStorage.length;index++) {
      const key=localStorage.key(index);if(!key?.startsWith(REQUEST_PREFIX))continue;
      const context=JSON.parse(key.slice(REQUEST_PREFIX.length)),request_id=pendingRequestId(context);
      if(request_id&&!entries.has(jobKey(context)))entries.set(jobKey(context),{...context,request_id});
    }
  }catch { /* A malformed local entry cannot start an analysis. */ }
  for(const entry of state.requests.values())if(entry.live)entries.set(entry.key,jobContext(entry.payload));
  let regenerationNumber=0;
  for(const context of entries.values()) {
    if(!visibleContext(context)||context.event_id!==state.selected)continue;
    const description=context.question?'Вопрос: '+context.question:context.task==='insight'?'Основной анализ':'Подбор источников';
    const attempt=context.regenerate?' · перегенерация '+(++regenerationNumber):'';
    section.append(button(description+attempt+' · открыть задание',()=>selectEvent(context.event_id,context.mode,context.question,context.task,
      {parent_run_id:context.parent_run_id,regenerate:context.regenerate,request_id:context.request_id,resumeOnly:!!pendingJob(context)})));
  }
  section.hidden=section.children.length===0;
}
function showJobProgress(job) {
  let feedback=$('analysis-feedback');
  if (!feedback) {feedback=node('div');feedback.id='analysis-feedback';feedback.tabIndex=-1;$('detail').append(feedback);}
  feedback.hidden=false;feedback.className='analysis-feedback working';feedback.setAttribute('role','status');
  let stage=$('job-stage');
  if (!stage) {stage=node('p');stage.id='job-stage';feedback.append(stage);}
  stage.textContent=job.state==='queued'?'Задание в очереди. Можно перезагрузить страницу — обработка сохранится.':job.progress?.message||'Выполняем сохранённое задание…';
  if(job.context?.question)stage.textContent+=' Вопрос: «'+job.context.question+'».';
}
async function waitForJob(job,version,entry=null) {
  const deadline=Date.now()+20*60*1000;
  const resumed=job.resumed;
  while (true) {
    if (!entry && version!==undefined && version!==state.version) return null;
    if (entry) {
      entry.job=job;
      if (entry.live&&job.state!=='done') articleStatus(entry.payload,job.state==='failed'?'generation_unavailable':'working');
    }
    if (job.state==='done') {if(resumed&&job.run) job.run.restored_from_cache=true;return job.run||job.report;}
    if (job.state==='failed') {const error=new Error(job.error?.message||'Задание не завершено. Можно повторить действие.');error.jobFailed=true;throw error;}
    if (Date.now()>deadline) throw new Error('Анализ ещё выполняется на сервере. Вернитесь к этой новости позже: повторная генерация не нужна.');
    if (entry ? state.visibleRequestKey===entry.key && visibleContext(entry.payload) : version!==undefined) showJobProgress(job);
    await new Promise(resolve=>setTimeout(resolve,1000));
    job=await request(`/api/jobs/${job.job_id}`);
  }
}
async function analysisRequest(payload,version) {
  const existing=activeArticleRequest(payload.event_id,payload);
  if (existing) {state.visibleRequestKey=existing.key;if(existing.job)showJobProgress(existing.job);return existing.promise;}
  const entry={key:jobKey(payload),
    articleKey:articleKey(payload.event_id,payload),payload:{...payload},live:payload.mode==='live',card:state.run};
  state.requests.set(entry.key,entry);state.visibleRequestKey=entry.key;
  if(entry.live)articleStatus(payload,'working');
  entry.promise=performAnalysisRequest(payload,entry).then(run=>{
    entry.finished=true;
    if (run) {assertRunShape(run);rememberRun(run);if(entry.live)articleStatus(entry.payload,run.status);}
    return run;
  }).catch(error=>{entry.finished=true;if(entry.live)articleStatus(entry.payload,'generation_unavailable');throw error;})
    .finally(()=>{if(state.requests.get(entry.key)===entry)state.requests.delete(entry.key);renderBackgroundTasks();});
  return entry.promise;
}
async function performAnalysisRequest(payload,entry) {
  const key=jobKey(payload),stored=pendingJob(payload);
  let job;
  if (stored) {
    try {
      const candidate=await request(`/api/jobs/${stored}`);
      const context=candidate.context||{};
      if(!context.mode || jobKey(context)!==key)throw new Error('Сохранённое задание относится к другому вопросу или контексту. Оно не использовано; новая генерация не запускалась.');
      job=candidate;job.resumed=true;
    } catch(error) {
      if ([400,404].includes(error.status)) clearPendingJob(key,stored);
      else throw error;
    }
  }
  if(!job && payload.resumeOnly)throw new Error('Задание уже завершено или недоступно. Автоматическая новая генерация не запускалась.');
  if (!job) {
    if(payload.mode==='live')try {localStorage.setItem(requestKey(payload),payload.request_id);} catch {}
    job=await request('/api/insights',{...payload,async:payload.mode==='live'});
  }
  // The synchronous contract remains supported for older servers and source-only work.
  if (job.run_id) {clearRequestId(payload);return job;}
  if (!/^[a-f0-9]{32}$/.test(job.job_id||'')) throw new Error('Сервер не вернул идентификатор задания.');
  entry.live=true;
  try {localStorage.setItem(key,job.job_id);} catch {}
  renderBackgroundTasks();
  try {
    const run=await waitForJob(job,undefined,entry);
    if (run) {clearPendingJob(key,job.job_id);clearRequestId(payload);}
    return run;
  } catch(error) {
    if (error.jobFailed) {clearPendingJob(key,job.job_id);clearRequestId(payload);}
    throw error;
  }
}
function rememberRun(run) {
  if (!isAnalysisAttempt(run)||!['complete','validation_failed','generation_unavailable','insufficient_evidence'].includes(run.status) || !run.run_id || run.question) return;
  const row=run.company_id===state.company&&run.role===state.role?state.feed.find(row=>row.event.event_id===run.event.event_id):null;
  if (row?.context_fingerprint && row.context_fingerprint===run.context_fingerprint) {
    row.latest_run_id=run.run_id;row.latest_status=run.status;
    if(run.status==='complete')row.cached_run_id=run.run_id;
  }
  try {
    localStorage.setItem(attemptKey(run.event.event_id,run),run.run_id);
    if(run.status==='complete')localStorage.setItem(resultKey(run.event.event_id,run),run.run_id);
  } catch { /* Storage is optional. */ }
}
async function savedRun(eventId) {
  const row=state.feed.find(row=>row.event.event_id===eventId);
  let runId=row?.latest_run_id;
  if (!runId) try { runId=localStorage.getItem(attemptKey(eventId))||row?.cached_run_id||localStorage.getItem(resultKey(eventId)); } catch {runId=row?.cached_run_id;}
  if (!runId || !/^[a-f0-9]{32}$/.test(runId)) return null;
  try {
    const run=await request(`/api/runs/${runId}`);
    const event=row?.event;
    if (isAnalysisAttempt(run)&&['complete','validation_failed','generation_unavailable','insufficient_evidence'].includes(run.status) && run.company_id===state.company && run.role===state.role &&
        run.event.event_id===eventId && run.event.content_sha256===event?.content_sha256 && run.analysis_as_of===state.feedAsOf &&
        row.context_fingerprint && run.context_fingerprint===row.context_fingerprint) {
      run.restored_from_cache=true;
      run.trace=[...run.trace,{step:'restore_result',reason:'Открыт последний сохранённый результат анализа.'}];
      return run;
    }
  } catch { /* Missing/withdrawn results fall back to a fresh local source search. */ }
  return null;
}
function workspace(open=true) {
  $('home').hidden=open; $('workspace').hidden=!open;
  $('home-nav').classList.toggle('active',!open); $('news-nav').classList.toggle('active',open);
  $('home-nav').setAttribute('aria-current',open?'false':'page');
  $('news-nav').setAttribute('aria-current',open?'page':'false');
}
function updateProfile() {
  const profile=state.profiles.find(p=>p.company_id===state.company); if (!profile) return;
  $('home-industry').textContent=profile.company.industry_name;
  const credit=profile.company.products.credit?'В базе отмечен кредитный продукт':'Кредитный продукт в базе не отмечен';
  $('home-profile').textContent=`${profile.company_id} · ОКВЭД ${profile.company.industry_okved_code} · Регион регистрации: ${profile.company.region}. ${credit}.`;
  if ($('company-description')) $('company-description').textContent=`${profile.company.industry_name}`;
  const relationships=$('home-relations');
  if(relationships&&profile.payments) {
    const p=profile.payments;
    relationships.textContent=`Внешних исходящих контрагентов: ${p.outgoing_external_counterparty_count}; входящих: ${p.incoming_external_counterparty_count}. Самопереводы учтены отдельно. Период и валюта платежей не установлены; текущие закупки и долг этими связями не подтверждаются.`;
  }
  const network=$('home-network');
  if(network) {
    network.replaceChildren();
    const labels={'10':'Пищевое производство','23':'Минеральные материалы','24':'Металлургия',
      '25':'Металлические изделия','41':'Строительство зданий','42':'Инфраструктурное строительство',
      '43':'Строительные работы','46':'Оптовая торговля','47':'Розничная торговля',
      '49':'Сухопутный транспорт','52':'Складирование и логистика','64':'Финансовые услуги',
      '33':'Ремонт оборудования','62':'Разработка ПО и ИТ','63':'Информационные услуги'};
    for(const [key,title] of [['outgoing','Исходящие связи'],['incoming','Входящие связи']]) {
      const part=profile.network?.[key];if(!part?.industry_divisions?.length)continue;
      const section=node('div');section.append(node('strong',title));
      section.append(list(part.industry_divisions.slice(0,3).map(row=>`${labels[row.okved_division]||'ОКВЭД '+row.okved_division} — ${row.company_count}`)));
      section.append(node('p',`Профиль известен у ${part.classified_count} из ${part.unique_company_count} контрагентов. Это число связей, не доля денежных оборотов.`,'small-note'));
      network.append(section);
    }
    const group=profile.network?.group;
    if(group?.unique_company_count>1) network.append(node('p',`По данным набора связаны компании: ${group.unique_company_count}. Состав группы, контроль и полномочия для общего решения нужно уточнить.`,'small-note'));
  }
}
async function loadFeed() {
  const version=++state.version; stopProgress(); state.selected=null; state.run=null; state.busy=false;state.visibleRequestKey=null;
  ++state.searchVersion; clearTimeout(state.searchTimer); state.searchRows=null;state.searchBusy=false;state.searchError='';$('search').value='';
  state.feed=[]; state.feedAsOf=null;
  updateActions();
  $('events').replaceChildren(node('p','Загружаем события для выбранной компании…','muted'));
  $('source-count').textContent='Загружаем публикации…';
  const empty=node('div',null,'empty');empty.append(node('span','ОТ НОВОСТИ К РЕШЕНИЮ','eyebrow'),node('h2','Выберите событие в ленте'),node('p','Покажем факт, банковский контекст и границы выводов для вашей компании.'),node('span','Откройте событие, чтобы посмотреть источники и подготовить анализ.','small-note'));
  $('detail').replaceChildren(empty);
  try {
    const data=await request(`/api/news?company_id=${encodeURIComponent(state.company)}&role=${state.role}`);
    if (version!==state.version) return;
    state.feed=data.items; state.feedAsOf=data.as_of; renderFeed(); renderNavigation(); $('source-count').textContent=`Событий: ${state.feed.length}`;
  } catch(error) { if (version===state.version) notice(error.message,true); }
}
function renderFeed() {
  const focusedEvent=document.activeElement?.dataset?.eventId;
  const search=$('search').value.trim().toLowerCase(); const showLow=$('show-low').checked;
  const rows=(search?(state.searchRows||[]):state.feed).filter(row=>showLow||row.business_score>0);
  if ($('feed-caption')) $('feed-caption').textContent=state.searchBusy?'Ищем подходящие события…':state.searchError||`Показано ${rows.length} из ${state.feed.length} публикаций`;
  $('events').replaceChildren();
  if (state.searchError) { $('events').append(button('Повторить поиск',()=>searchFeed())); return; }
  if (state.searchBusy) { $('events').append(node('p','Ищем близкие по смыслу события…','muted'));return; }
  if (!rows.length) { $('events').append(node('p',state.feed.length?'Для этих фильтров события не найдены. Можно показать все публикации.':'Снимок пока пуст. Нажмите «Обновить источники».','muted')); return; }
  for (const row of rows) {
    const item=button('',()=>selectEvent(row.event.event_id),'event'+(row.event.event_id===state.selected?' selected':''));
    item.setAttribute('aria-pressed',String(row.event.event_id===state.selected));
    item.dataset.eventId=row.event.event_id;
    const meta=node('div',null,'meta'); meta.append(node('span',dateText(row.event.published_at)),node('span',row.event.publisher||'Источник'));
    item.append(meta,node('h3',row.event.title));
    const relevance=row.relevance_level==='high'?'Высокая релевантность':row.relevance_level==='medium'?'Есть связь с бизнесом':'Слабая связь с профилем';
    item.append(node('span',relevance,'badge '+row.relevance_level));
    item.append(node('p',row.reasons[0]?.text||'Значимой связи с доступными признаками не найдено.','event-reason'));
    const status=state.articleStates.get(articleKey(row.event.event_id))?.status||row.latest_status;
    const statusLabels={working:'Анализ выполняется…',complete:'Анализ готов',answered:'Ответ готов',
      validation_failed:'Анализ не прошёл проверку',generation_unavailable:'Ошибка анализа',insufficient_evidence:'Недостаточно источников'};
    if(statusLabels[status])item.append(node('span',statusLabels[status],'article-state '+(status==='working'?'working':['complete','answered'].includes(status)?'ready':'failed')));
    item.append(node('span',status&&['validation_failed','generation_unavailable','insufficient_evidence'].includes(status)?'Открыть ошибку и повторить →':row.cached_run_id?'Открыть готовый анализ →':'Открыть новость и источники →','event-open'));
    $('events').append(item);
    if(focusedEvent===row.event.event_id)item.focus({preventScroll:true});
  }
}
async function searchFeed() {
  clearTimeout(state.searchTimer);
  const query=$('search').value.trim(),version=++state.searchVersion;
  state.searchError='';
  if (!query) {state.searchRows=null;state.searchBusy=false;renderFeed();return;}
  state.searchBusy=true;state.searchRows=[];renderFeed();
  try {
    const data=await request(`/api/search?company_id=${encodeURIComponent(state.company)}&role=${state.role}&as_of=${state.feedAsOf}&q=${encodeURIComponent(query)}`);
    if (version!==state.searchVersion) return;
    state.searchRows=data.items;state.searchBusy=false;renderFeed();
  } catch(error) {
    if (version!==state.searchVersion) return;
    state.searchBusy=false;state.searchError=error.message;renderFeed();
  }
}
function scheduleSearch() {
  clearTimeout(state.searchTimer);++state.searchVersion;
  if (!$('search').value.trim()) return searchFeed();
  state.searchBusy=true;state.searchRows=[];state.searchError='';renderFeed();
  state.searchTimer=setTimeout(searchFeed,250);
}
function block(title) { const element=node('section',null,'detail-block'); element.append(node('h3',title)); return element; }
function renderFeedback(run) {
  const section=block('Насколько полезен этот ответ?');section.id='feedback-block';
  const actions=node('div',null,'question-actions');
  const status=node('p','','small-note');status.id='feedback-status';status.setAttribute('role','status');
  const explanation=node('div');explanation.hidden=true;
  const label=node('label','Что стоит исправить?');label.setAttribute('for','feedback-comment');
  const comment=node('textarea',null,'question');comment.id='feedback-comment';comment.maxLength=1000;
  const idKey=`alfa.feedback.id:${run.run_id}`;
  let currentRating='incorrect';
  const send=async rating=>{
    for(const control of actions.children) control.disabled=true;status.textContent='Сохраняем оценку…';
    try {
      let id;try{id=localStorage.getItem(idKey);}catch{}
      if(!/^[a-f0-9]{32}$/.test(id||'')) {id=crypto.randomUUID().replaceAll('-','');try{localStorage.setItem(idKey,id);}catch{}}
      await request('/api/feedback',{feedback_id:id,run_id:run.run_id,rating,comment:comment.value.trim()});
      currentRating=rating;
      for(const control of actions.children) control.setAttribute('aria-pressed',String(control.dataset.rating===rating));
      status.textContent='Спасибо, оценка сохранена. Команда увидит её вместе с этим результатом.';
    } catch(error) {status.textContent=error.message;}
    finally {for(const control of actions.children) control.disabled=false;}
  };
  for(const [rating,text] of [['helpful','Полезно'],['incorrect','Есть ошибка'],['irrelevant','Не относится к компании']]) {
    const control=button(text,()=>{explanation.hidden=rating==='helpful';return send(rating);});
    control.dataset.rating=rating;control.setAttribute('aria-pressed','false');actions.append(control);
  }
  explanation.append(label,comment,button('Отправить уточнение',()=>send(currentRating)));
  section.append(actions,explanation,status);return section;
}
function list(items) { const element=node('ul'); for(const item of items) element.append(node('li',item)); return element; }
function featureLabel(field,run) {
  const credit=state.profiles.find(profile=>profile.company_id===run.company_id)?.company.products.credit;
  const labels={'company.industry_okved_code':'Отрасль компании','company.industry_name':'Вид деятельности компании',
    'company.products.credit':credit?'В базе отмечен кредитный продукт; текущий долг не установлен':'Кредитный продукт в базе не отмечен; отсутствие долга этим не установлено',
    'company.products.leasing':'Маркер лизинга в базе; условия договора не установлены',
    'payments.outgoing_external_counterparty_count':'Внешние исходящие платёжные связи',
    'payments.incoming_external_counterparty_count':'Внешние входящие платёжные связи',
    'payments.outgoing_external_missing_profile_counterparty_count':'Неполнота сведений об исходящих контрагентах',
    'payments.incoming_external_missing_profile_counterparty_count':'Неполнота сведений о входящих контрагентах',
    'payments.self_transfer_relation_rows':'Самопереводы компании в наборе данных',
    'network.outgoing.industry_divisions':'Отраслевая структура исходящих платёжных связей',
    'network.incoming.industry_divisions':'Отраслевая структура входящих платёжных связей',
    'network.group.unique_company_count':'Компании с общим GROUP_ID',
    'company.employee_band_fns':'Сведения о численности сотрудников','company.employee_band_analytics':'Оценка численности сотрудников'};
  return labels[field]||'Признак из профиля компании';
}
function issueText(issue) {
  const labels={bank_quote_not_found:'Цитата банка не совпала с указанным фрагментом.',news_quote_not_found:'Цитата новости не совпала с источником.',
    unknown_or_missing_evidence_id:'Ответ ссылается на неизвестный или неверный фрагмент.',
    bank_segment_qualifier_missing:'Потеряна оговорка о сегменте автомобилей.',bank_does_not_establish_clothing_demand:'Источник не устанавливает динамику спроса на одежду.',
    cross_segment_scope_not_explained:'Не объяснены границы переноса между сегментами.',clothing_summary_overstates_macro_evidence:'Краткий вывод слишком уверенно переносит макростатистику на одежду.',
    title_overstates_clothing_evidence:'Заголовок представляет неподтверждённую динамику одежды как факт.',
    active_debt_not_established:'Действующий долг клиента не подтверждён.',bank_publication_date_missing:'Не указана точная дата позиции банка.',bank_publication_date_mismatch:'Дата позиции банка не совпала с датой источника.',bank_observation_period_missing_or_changed:'Период наблюдений банка потерян или заменён датой публикации.',
    semantic_review_not_approved:'Вывод модели не подтверждён источниками.',draft_requests_more_evidence:'Модели не хватило доказательств.',
    credit_cost_requires_contract_condition:'Вывод о росте кредитных платежей не подтверждён условиями договора компании.',
    forecast_not_supported_by_quote:'Цитата не подтверждает прогноз, который сформулировала модель.',
    unreviewed_bank_numeric_claim:'Модель использовала непроверенные числа из документа.',
    unsupported_quantified_company_effect:'Числовой вывод о компании не подтверждён данными.',
    false_zero_flag_inference:'Признак продукта не подтверждает отсутствие долга у компании.',
    intragroup_payment_not_established:'Модель необоснованно назвала перевод внутригрупповым.',
    company_impact_must_be_conditional:'Модель представила возможное влияние на компанию как установленный факт.',
    unknown_company_feature:'В выводе использован отсутствующий признак компании.',
    news_number_not_supported_by_quote:'Число в выводе не подтверждается цитатой новости.'};
  return labels[issue]||(/^[a-z][a-z0-9_]*$/.test(issue)?'Ответ не удовлетворяет требованиям проверки источников.':issue);
}
function renderClaims(title,items,run) {
  const section=block(title);
  for(const item of items||[]) {
    const card=node('div',null,'claim');
    if (item.publication_date && !item.text.includes(item.publication_date)) card.append(node('p','Позиция от '+dateText(item.publication_date),'muted'));
    card.append(node('p',item.text));
    if (item.quote) {
      const quote=node('details',null,'quote-detail'); quote.append(node('summary','Дословная цитата'),node('p',`«${item.quote}»`,'muted'));card.append(quote);
    }
    if (item.company_fields?.length) card.append(node('p',[...new Set(item.company_fields.map(field=>featureLabel(field,run)))].join(' · '),'feature-label'));
    for (const id of item.evidence_ids||[]) {
      const source=run.bank_evidence.find(source=>source.evidence_id===id);
      if (source) {
        const citation=link(`Альфа-Банк · ${dateText(source.publication_date)} · стр. ${source.page} ↗`,source.page_url);citation.title=source.title;card.append(citation);
      }
      else if (id===run.event.event_id) card.append(link(`${run.event.publisher} · ${dateText(run.event.published_at)} ↗`,run.event.source_url));
    }
    if (item.company_fields?.length || item.evidence_ids?.length) {
      const proof=node('details'); proof.append(node('summary','Основания вывода'));
      if (item.company_fields?.length) proof.append(node('p',item.company_fields.join(' · '),'field-ref'));
      if (item.evidence_ids?.length) proof.append(node('p',item.evidence_ids.join(', '),'field-ref'));
      card.append(proof);
    }
    section.append(card);
  }
  return section;
}
function evidenceBrief(title,item,run) {
  const card=node('section',null,'evidence-brief');card.append(node('h3',title),node('p',item.text));
  if(item.publication_date) card.append(node('p','Опубликовано '+dateText(item.publication_date),'muted'));
  for(const id of item.evidence_ids||[]) {
    const source=run.bank_evidence.find(source=>source.evidence_id===id);
    if(source) card.append(link(`Альфа-Банк · стр. ${source.page} ↗`,source.page_url));
    else if(id===run.event.event_id) card.append(link(`${run.event.publisher} · ${dateText(run.event.published_at)} ↗`,run.event.source_url));
  }
  return card;
}
async function showSource(source) {
  try {
    const data=await request(`/api/source?source_id=${encodeURIComponent(source.source_id)}&page=${source.page}`);
    $('source-title').textContent=`${data.metadata.title} · физическая страница ${data.page}`;
    $('source-text').textContent=data.text; $('source-warning').textContent=data.warning; $('source-dialog').showModal();
  } catch(error) { notice(error.message,true); }
}
function assertRunShape(run) {
  if (!run?.event || !Array.isArray(run.event.facts) || !Array.isArray(run.relevance?.reasons) ||
      !Array.isArray(run.bank_evidence) || !Array.isArray(run.trace) ||
      (run.status==='answered' && (!run.answer || typeof run.answer.title!=='string' || (!Array.isArray(run.answer.items)&&!Array.isArray(run.answer.points)))) ||
      (run.status==='complete' && (!run.insight || !['news_facts','bank_positions','interpretations','risks','opportunities','actions','uncertainties'].every(key=>Array.isArray(run.insight[key]))))) {
    throw new Error('Сервер вернул неполную карточку анализа. Предыдущая карточка сохранена.');
  }
}
function renderRun(run) {
  assertRunShape(run);
  // Build off-screen first: rendering errors must not erase the current card.
  const detail=node('div',null,'detail-content');
  const meta=node('div',null,'story-meta'); meta.append(node('span',`Опубликовано ${dateText(run.event.published_at)} · ${run.event.publisher}`),link('Первоисточник ↗',run.event.source_url));
  detail.append(meta,node('h2',run.event.title,'story-title'));
  if(run.event.source_warning) detail.append(node('p',run.event.source_warning,'scope-note'));
  detail.append(node('p',`Анализ на ${dateText(run.analysis_as_of)}${run.event.event_date?' · Дата события: '+dateText(run.event.event_date):''}`,'story-date-note'));
  const why=node('details',null,'why-company');why.append(node('summary','Почему новость выбрана для компании'));
  why.append(list(run.relevance.reasons.map(x=>x.text)));
  if (!run.relevance.reasons.length) why.append(node('p','Доступных признаков для обоснованной персонализации недостаточно.'));
  const outcome=node('section',null,'analysis-outcome'); outcome.id='analysis-outcome'; outcome.tabIndex=-1;
  outcome.setAttribute('aria-label','Результат анализа');
  const facts=run.status==='complete' ? renderClaims('Факт новости',run.insight.news_facts,run) : block('Факт новости · выдержки из источника');
  if (run.status!=='complete') for (const fact of run.event.facts.slice(0,3)) facts.append(node('p',fact.text));
  if (run.status==='complete') {
    outcome.classList.add('complete');
    outcome.append(node('h2','Инсайт готов'),node('p',`${run.company_id} · ${run.role==='cfo'?'Финансовый директор':'Руководитель бизнеса'}`,'result-context'));
    outcome.append(node('h3',run.insight.title||'Персонализированная аналитика'));
    if (run.question) outcome.append(node('p','Ваш вопрос: '+run.question,'muted'));
    if (run.insight.interpretations[0]) {
      outcome.append(node('p','Условная интерпретация · влияние на компанию требует проверки','interpretation-label'));
      outcome.append(node('p',run.insight.interpretations[0].text,'insight-summary'));
    }
    const next=renderClaims('Следующий шаг для компании',run.insight.actions,run);next.id='analysis-next-step';next.tabIndex=-1;next.classList.add('next-step');
    outcome.append(next);
    const modelSteps=run.trace.filter(step=>['draft_result','model_review'].includes(step.step));
    const cached=run.restored_from_cache || modelSteps.length>0 && modelSteps.every(step=>step.cached===true);
    outcome.append(node('p',cached?'Открыт сохранённый анализ.':'Анализ проверен по источникам.','result-status'));
    const overview=node('div',null,'evidence-overview');
    if(run.insight.news_facts[0]) overview.append(evidenceBrief('Факт новости',run.insight.news_facts[0],run));
    if(run.insight.bank_positions[0]) overview.append(evidenceBrief('Датированная позиция банка',run.insight.bank_positions[0],run));
    detail.append(overview);
    const breakdown=node('details',null,'analysis-breakdown');breakdown.id='analysis-breakdown';breakdown.open=false;
    breakdown.append(node('summary','Разбор: факт новости, позиция банка, риски и возможности'));
    breakdown.append(facts,renderClaims('Позиция Альфа-Банка',run.insight.bank_positions,run));
    breakdown.append(renderClaims('Условная интерпретация для компании',run.insight.interpretations,run));
    breakdown.append(renderClaims('Риски',run.insight.risks,run));
    breakdown.append(renderClaims('Возможности',run.insight.opportunities,run));
    const uncertainty=block('Неопределённость'); uncertainty.append(list(run.insight.uncertainties)); breakdown.append(uncertainty);
    if (run.insight.scope_note) breakdown.append(node('p',run.insight.scope_note,'scope-note'));
    outcome.append(breakdown);
  } else if (run.status==='answered') {
    outcome.classList.add('complete');
    outcome.append(node('h2','Ответ на вопрос готов'));
    if(run.answer_origin!=='rules') outcome.append(node('p',run.question,'muted'));
    outcome.append(node('h3',run.answer.title),node('p',run.answer.summary,'insight-summary'));
    if (run.answer.scope_note) outcome.append(node('p',run.answer.scope_note,'scope-note'));
    if (run.answer.items) for (const item of run.answer.items) {
      const part=block(item.text); part.append(node('p',item.purpose)); outcome.append(part);
    }
    if (run.answer.points) outcome.append(renderClaims('Пояснение по источникам',run.answer.points,run));
    if (run.answer.limits?.length) outcome.append(list(run.answer.limits));
    if (run.answer.uncertainties?.length) outcome.append(list(run.answer.uncertainties));
    const cached=run.trace.filter(step=>['draft_result','model_review'].includes(step.step));
    outcome.append(node('p',run.answer_origin==='rules'?'Список составлен по доступным данным компании.':
      run.restored_from_cache||cached.length&&cached.every(step=>step.cached)?'Открыт сохранённый ответ.':'Ответ проверен по источникам.','result-status'));
  } else {
    detail.append(facts);
    let message='Откройте материалы банка или подготовьте анализ для вашей компании.';
    if (run.status==='insufficient_evidence') message='Источников недостаточно для обоснованного вывода. Уточните вопрос или проверьте доступные материалы.';
    if (run.status==='low_relevance') message='Доступные данные не подтверждают значимость события для компании. Можно изучить первоисточник.';
    if (run.error) message=run.error.message+(run.error.retry_at?' Следующий период: '+run.error.retry_at:'');
    if (run.stop_detail?.message) message=run.stop_detail.message;
    const headings={sources_ready:'Источники готовы',insufficient_evidence:'Анализ остановлен: недостаточно доказательств',validation_failed:'Ответ не прошёл проверку',stale_result:'Нужна актуальная проверка',generation_unavailable:'Техническая ошибка генерации',low_relevance:'Связь с компанией недостаточна'};
    outcome.append(node('h3',headings[run.status]||'Результат анализа'),node('p',message,'notice'));
    if (run.stop_detail?.issues?.length) outcome.append(list(run.stop_detail.issues.map(issueText)));
    if (run.stop_detail?.next_step) outcome.append(node('p','Следующий шаг: '+run.stop_detail.next_step));
    if (['generation_unavailable','insufficient_evidence','validation_failed','stale_result'].includes(run.status)) outcome.classList.add('error');
    if (run.status==='generation_unavailable') outcome.classList.add('technical-error');
  }
  detail.append(outcome);
  if(run.news_context?.context_incomplete)detail.append(node('p',run.news_context.context_warning,'scope-note'));
  if (run.status==='answered') {
    const newsContext=node('details');newsContext.append(node('summary','Факт новости · контекст вопроса'),facts);detail.append(newsContext);
  }
  detail.append(why);
  const toolbar=node('div',null,'toolbar');
  const ready=run.status==='complete',returning=run.status==='answered';
  const failed=['generation_unavailable','validation_failed','insufficient_evidence','stale_result'].includes(run.status);
  const hasMain=state.feed.some(row=>row.event.event_id===run.event.event_id && row.cached_run_id);
  const regenerate=()=>selectEvent(run.event.event_id,'live',run.question||'',run.task==='sources'?'insight':run.task,{regenerate:true});
  const generate=button(ready?'Показать готовый инсайт':returning?(hasMain?'Открыть основной анализ':'Вернуться к новости'):failed?'Перегенерировать':'Подготовить анализ',()=>ready?focusOutcome():failed?regenerate():selectEvent(run.event.event_id,returning?'sources':'live'),ready||returning?'secondary':'primary');
  generate.id='generate-insight'; generate.setAttribute('data-analysis-action','');
  generate.dataset.requiresLlm=String(!ready&&!returning); generate.dataset.idleLabel=generate.textContent;
  generate.disabled=!ready&&!returning&&!state.status.llm.live_enabled;
  generate.title=ready?'Перейти к готовому результату без нового запроса':returning?'Открыть основной результат или источники без генерации':state.status.llm.live_enabled?'Подготовить персональный анализ с проверкой источников':'Анализ станет доступен после настройки подключения';
  if(failed){outcome.setAttribute('role','alert');outcome.append(generate);}else toolbar.append(generate);
  if(ready||returning&&run.answer_origin==='llm') {
    const again=button('Перегенерировать',regenerate);again.id='regenerate-insight';again.setAttribute('data-analysis-action','');again.dataset.requiresLlm='true';again.dataset.idleLabel=again.textContent;toolbar.append(again);
  }
  if(!ready&&!returning&&!state.status.llm.live_enabled) toolbar.append(node('span','Доступен просмотр источников. Подключение анализа настраивается на сервере.','muted'));
  detail.append(toolbar);
  const feedback=node('div',null,'analysis-feedback'); feedback.id='analysis-feedback'; feedback.hidden=true; feedback.tabIndex=-1;
  feedback.setAttribute('role','status'); feedback.setAttribute('aria-live','polite'); feedback.setAttribute('aria-atomic','true'); detail.append(feedback);
  const sources=node('section',null,'detail-block');
  sources.append(node('p','Дата позиции указана у каждого документа. Для проверки чисел откройте оригинал PDF.','muted'));
  for(const source of run.bank_evidence) {
    const card=node('article',null,'source-card');
    card.append(node('span',`${dateText(source.publication_date)} · стр. ${source.page}`,'muted'),node('h3',source.title),node('p',source.text.slice(0,230)+'…'));
    const links=node('div',null,'source-links'); links.append(button('Открыть текст страницы',()=>showSource(source),'text-button'),link('PDF ↗',source.page_url));
    card.append(links); const reference=node('details'); reference.append(node('summary','Идентификатор фрагмента'),node('p',source.evidence_id,'field-ref')); card.append(reference); sources.append(card);
  }
  if (run.bank_evidence.length) {
    const sourceDetails=node('details',null,'source-browser');sourceDetails.id='bank-sources';sourceDetails.tabIndex=-1;
    sourceDetails.open=run.status==='sources_ready';sourceDetails.append(node('summary',`Банковские источники · ${run.bank_evidence.length}`),sources);detail.append(sourceDetails);
  }
  const followup=block('Уточнить анализ');followup.classList.add('followup');
  const gaps=button('Каких данных не хватает?',()=>selectEvent(run.event.event_id,'sources','Каких данных не хватает для полного вывода?','missing_data'));
  gaps.setAttribute('data-analysis-action','');gaps.dataset.allowConcurrent='true'; followup.append(gaps,node('p','Уточните вывод или узнайте, какие данные нужны для следующего решения.','muted'));
  const input=node('textarea',null,'question');input.id='followup-question'; input.maxLength=500; input.placeholder='Например: какие выводы источники подтверждают для нашей компании?'; input.setAttribute('aria-label','Уточняющий вопрос');
  const label=node('label','Ваш вопрос','followup-label');label.setAttribute('for',input.id);
  const questionError=node('p',null,'question-error');questionError.id='question-error';questionError.hidden=true;questionError.setAttribute('role','alert');
  input.setAttribute('aria-describedby','question-error');
  const question=()=>{
    const value=input.value.trim();questionError.hidden=Boolean(value);input.setAttribute('aria-invalid',String(!value));
    if(!value){questionError.textContent='Введите вопрос или выберите «Каких данных не хватает?». ';input.focus();}
    return value;
  };
  const ask=button('Задать вопрос с проверкой источников',()=>{
    const value=question();if (value) selectEvent(run.event.event_id,'live',value);
  }); ask.setAttribute('data-analysis-action',''); ask.dataset.requiresLlm='true';ask.dataset.allowConcurrent='true';
  const find=button('Найти источники по вопросу',()=>{const value=question();if(value)selectEvent(run.event.event_id,'sources',value,'sources');}); find.setAttribute('data-analysis-action','');find.dataset.allowConcurrent='true';
  const questionActions=node('div',null,'question-actions');
  if (state.status.llm.live_enabled) questionActions.append(ask);
  questionActions.append(find);
  followup.append(label,input,questionError,questionActions); detail.append(followup);
  const trace=node('details',null,'trace-browser');trace.id='analysis-trace'; trace.open=false; trace.append(node('summary',`Ход анализа · ${run.trace.length} шагов`));
  const steps=node('ol',null,'trace'); const labels={route:'Задача',data_gaps:'Недостающие данные',plan:'План',retrieve:'Поиск источников',reuse_sources:'Источники готового вывода',normalize_features:'Уточнение полного пути признака',candidate_recheck:'Повторная проверка прежнего черновика',draft:'Черновик модели',generation_recovery:'Повтор после незавершённого ответа',draft_result:'Ответ модели',check:'Проверка цитат и признаков',model_review:'Проверка обоснованности',replan:'Дополнительный поиск',revise:'Исправление по найденным источникам',response_error:'Диагностика ответа LLM',stop:'Остановка',restore_result:'Открытие сохранённого результата'};
  for(const step of run.trace) {
    const item=node('li'); item.append(node('strong',labels[step.step]||step.step));
    if (step.step==='candidate_recheck') item.append(node('div','Прежний JSON рассматривается как непроверенный черновик; старое одобрение не используется.'));
    if (step.round) item.append(node('div',`Раунд: ${step.round}`));
    if (step.model) item.append(node('div',`Модель: ${step.model}`));
    if (step.stage) item.append(node('div',`Этап: ${step.stage}`));
    if (step.requested_max_tokens) item.append(node('div',`Лимит ответа: ${step.requested_max_tokens} токенов`));
    if (step.context_chars) item.append(node('div',`Контекст: ${step.context_chars} символов`));
    if (step.query) item.append(node('div',step.query));
    if (step.queries?.length) item.append(list(step.queries));
    if (step.unavailable_queries?.length) { item.append(node('div','За пределами банковского корпуса; нужны данные клиента:')); item.append(list(step.unavailable_queries)); }
    if (step.reason) item.append(node('div',step.reason));
    if (step.next_round_will_run===false) item.append(node('div','Следующий раунд не выполнялся: достигнут предел раундов.'));
    if (step.evidence_ids) item.append(node('div',`Найдено фрагментов: ${step.evidence_ids.length}`));
    if (step.issues?.length) item.append(node('div','Замечания: '+step.issues.map(issueText).join(' ')));
    else if (step.step==='check') item.append(node('div','Цитаты, поля компании и обязательная структура проверены.'));
    if (typeof step.approved==='boolean') item.append(node('div',step.approved?'Модельная проверка: одобрено.':'Модельная проверка: отклонено.'));
    if (typeof step.cached==='boolean') item.append(node('div',step.cached?'На этом шаге использован кеш.':'При формировании этого результата был выполнен API-запрос.'));
    if (step.diagnostics && Object.keys(step.diagnostics).length) item.append(node('pre',JSON.stringify(step.diagnostics,null,2),'field-ref'));
    steps.append(item);
  }
  trace.append(steps); detail.append(trace);
  if(['complete','answered'].includes(run.status)) detail.append(renderFeedback(run));
  $('detail').replaceChildren(detail); state.run=run;renderNavigation();renderBackgroundTasks();
}
async function selectEvent(eventId,mode='sources',question='',task,options={}) {
  const keepCard=state.run?.event.event_id===eventId;
  const payload={company_id:state.company,event_id:eventId,role:state.role,mode,question,task,as_of:state.feedAsOf};
  if(options.regenerate)payload.regenerate=true;
  if (question||mode==='live') {
    try { payload.parent_run_id=state.run?.status==='complete'&&state.run.event.event_id===eventId?state.run.run_id:localStorage.getItem(resultKey(eventId)); } catch {}
  }
  if(Object.hasOwn(options,'parent_run_id'))payload.parent_run_id=options.parent_run_id;
  if(mode==='live')payload.request_id=options.request_id||(!payload.regenerate&&pendingRequestId(payload))||crypto.randomUUID().replaceAll('-','');
  Object.assign(payload,jobContext(payload));
  if(options.resumeOnly)payload.resumeOnly=true;
  if(state.busy && eventId===state.selected && state.visibleRequestKey===jobKey(payload))return;
  const version=++state.version; stopProgress(); state.selected=eventId; state.busy=true; state.visibleRequestKey=null;notice('');
  if (!keepCard) {
    const pending=activeArticleRequest(eventId,payload);
    state.run=null;renderFeed();
    if(pending?.card?.event.event_id===eventId)renderRun(pending.card);
    else $('detail').replaceChildren(node('div','Собираем банковские источники…','empty'));
  }
  updateActions(); startProgress(mode,version);
  try {
    let run=mode==='sources'&&!question&&(!task||task==='insight')&&!pendingJob(payload)?await savedRun(eventId):null;
    if (version!==state.version) return;
    if (!run) run=await analysisRequest(payload,version);
    if (version!==state.version) return;
    renderRun(run); rememberRun(run);
    requestAnimationFrame(()=>{
      if (version===state.version) focusOutcome();
    });
  } catch(error) {
    if (version===state.version) { state.busy=false; showRequestError(error,()=>selectEvent(eventId,mode,question,task,
      {...options,resumeOnly:false,parent_run_id:payload.parent_run_id,request_id:error.jobFailed?undefined:payload.request_id})); }
  } finally {
    if (version===state.version) { stopProgress(); state.busy=false; updateActions(); }
  }
}
async function start() {
  try {
    [state.status,state.profiles]=await Promise.all([request('/api/status'),request('/api/companies')]);
    $('gateway-status').textContent=state.status.llm.live_enabled?'Анализ доступен':'Источники и поиск';
    if ($('search-status')) $('search-status').textContent=!state.status.search?'Перезапустите сервер для проверки режима поиска':state.status.search.mode==='lexical'?'Поиск по словам':'Смысловой поиск';
    for(const profile of state.profiles) {
      const shortIndustry={'47.71':'Торговля одеждой','41.20.1':'Жилищное строительство',
        '25.11':'Производство металлоконструкций','49.41.1':'Грузовые перевозки','46.90':'Оптовая торговля'}[profile.company.industry_okved_code]||profile.company.industry_name;
      const option=node('option',`${profile.company_id} · ${shortIndustry}`);option.title=profile.company.industry_name; option.value=profile.company_id; $('company').append(option);
    }
    let remembered;
    try {remembered=JSON.parse(localStorage.getItem('alfa.news.context.v1'));} catch {}
    state.company=state.profiles.some(profile=>profile.company_id===remembered?.company_id)?remembered.company_id:state.profiles[0].company_id;
    state.role=['cfo','ceo'].includes(remembered?.role)?remembered.role:'cfo';
    $('company').value=state.company;$('role').value=state.role;
    workspace();updateProfile(); await loadFeed();
  } catch(error) { workspace(); notice(error.message,true); }
}
$('enter').addEventListener('click',()=>workspace()); $('home-nav').addEventListener('click',()=>workspace(false)); $('news-nav').addEventListener('click',()=>workspace());
$('company').addEventListener('change',()=>{state.company=$('company').value;rememberContext();updateProfile();loadFeed();});
$('role').addEventListener('change',()=>{state.role=$('role').value;rememberContext();loadFeed();});
$('search').addEventListener('input',scheduleSearch); $('show-low').addEventListener('change',renderFeed); $('close-source').addEventListener('click',()=>$('source-dialog').close());
$('refresh').addEventListener('click',async()=>{
  if (state.busy||state.refreshing) return;
  state.refreshing=true; $('refresh').disabled=true; notice('Обновляем новости из первоисточников…');
  try { let report=await request('/api/refresh',{limit:5,async:true}); if(report.job_id) report=await waitForJob(report);
    const rss=report.rss_text_imported?` Из текста издателя в RSS: ${report.rss_text_imported}.`:'';
    notice(`Новых: ${report.inserted}. Обновлено: ${report.updated}. Дубликатов: ${report.duplicate}.${rss} Недоступных публикаций: ${report.errors.length}.`,Boolean(report.errors.length)); await loadFeed(); }
  catch(error) {notice(error.message,true);} finally {state.refreshing=false;updateActions();}
});
start();

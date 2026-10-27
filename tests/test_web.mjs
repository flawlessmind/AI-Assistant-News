// Tests the actual app.js with a small DOM and synthetic HTTP responses. No network or LLM calls.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {test} from 'node:test';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';

const source=await readFile(new URL('../web/app.js',import.meta.url),'utf8');
const fixtureProfile=id=>({company_id:id,company:{industry_name:'Синтетическая отрасль UI-теста',industry_okved_code:id==='U_1595'?'47.71':'41.20.1',region:'Тестовый регион',products:{credit:id==='U_1595'}}});
const fixtureEvent={event_id:'test-news',title:'Синтетическое событие UI-теста',publisher:'Тестовый источник',source_url:'https://www.cbr.ru/test-only',published_at:'2026-10-16T12:00:00+03:00',event_date:null,content_sha256:'synthetic-hash',facts:[{text:'Синтетический факт для UI-теста.'}]};
const fixtureBank={evidence_id:'test-bank',source_id:'synthetic-bank',title:'Синтетический документ UI-теста',publication_date:'2026-09-07',page:1,page_url:'https://example.invalid/test-only#page=1',text:'Синтетический текст источника только для теста.'};
function fixtureRun(status='complete',overrides={}) {
  const claim={text:'Условный вывод синтетической UI-фикстуры.',evidence_ids:['test-bank'],company_fields:['company.industry_okved_code'],conditional:true};
  return {run_id:'a'.repeat(32),company_id:'U_1595',role:'cfo',question:'',analysis_as_of:'2026-10-21',context_fingerprint:'synthetic-current-context',event:structuredClone(fixtureEvent),status,
    relevance:{reasons:[{text:'Синтетическая причина релевантности.'}]},bank_evidence:[structuredClone(fixtureBank)],
    insight:status==='complete'?{title:'Синтетический инсайт UI-теста',news_facts:[{...claim,evidence_ids:['test-news']}],bank_positions:[claim],interpretations:[claim],risks:[claim],opportunities:[claim],actions:[claim],uncertainties:['Синтетические неизвестные.']}:null,
    trace:status==='complete'?[{step:'draft_result',cached:true},{step:'model_review',cached:true,approved:true,issues:[]},{step:'stop',reason:'synthetic complete'}]:[{step:'stop',reason:'synthetic source run'}],...overrides};
}
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};};
const flush=()=>new Promise(done=>setImmediate(done));
const multiEvents=[fixtureEvent,{...fixtureEvent,event_id:'other-news',title:'Другая синтетическая статья',content_sha256:'other-synthetic-hash'}];
const multiFeed=multiEvents.map(event=>({event,context_fingerprint:'synthetic-current-context',business_score:5,relevance_level:'high',reasons:[{text:'Синтетическая причина.'}]}));
const runFor=(eventId,status='complete',extra={})=>fixtureRun(status,{event:multiEvents.find(event=>event.event_id===eventId),
  run_id:(eventId==='test-news'?'a':'b').repeat(32),...extra});

class Element {
  constructor(tag,document) { this.tagName=tag;this.document=document;this.children=[];this.attributes={};this.dataset={};this.listeners={};this.className='';this._text='';this.hidden=false;this.disabled=false;this.value='';this.checked=false; }
  get textContent() { return this._text+this.children.map(child=>child.textContent).join(' '); }
  set textContent(value) { this._text=String(value);this.children=[]; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text='';this.children=[...children]; }
  setAttribute(key,value) { this.attributes[key]=String(value); }
  addEventListener(event,listener) { this.listeners[event]=listener; }
  get classList() { return {add:value=>{this.className+=' '+value;},toggle:(value,enabled)=>{const names=new Set(this.className.split(/\s+/).filter(Boolean));if(enabled)names.add(value);else names.delete(value);this.className=[...names].join(' ');}}; }
  querySelectorAll(selector) { const attribute=selector.slice(1,-1);return this.children.flatMap(child=>[...(Object.hasOwn(child.attributes,attribute)?[child]:[]),...child.querySelectorAll(selector)]); }
  focus(options) { this.document.activeElement=this;this.document.focusCalls.push({id:this.id,options}); }
  scrollIntoView(options) { this.document.scrollCalls.push({id:this.id,options}); }
  showModal() { this.open=true; }
  close() { this.open=false; }
}
class Document {
  constructor() {
    this.root=new Element('body',this);this.focusCalls=[];this.scrollCalls=[];
    for(const id of ['notice','home','workspace','analysis-navigation','home-nav','news-nav','home-industry','home-profile','detail','events','search','search-status','show-low','refresh','gateway-status','source-count','feed-caption','company-description','company','role','enter','close-source','source-title','source-text','source-warning','source-dialog']) {
      const node=this.createElement('div');node.id=id;this.root.append(node);
    }
    this.getElementById('role').value='cfo';
  }
  createElement(tag) { return new Element(tag,this); }
  getElementById(id) { const find=node=>node.id===id?node:node.children.map(find).find(Boolean);return find(this.root)||null; }
}
async function harness(options={}) {
  const document=new Document(),calls=[],timers=new Map(),storage=options.storage||new Map();let nextTimer=0,statusGets=0;
  const response=(data,status=200)=>({ok:status>=200&&status<300,status,json:async()=>structuredClone(data)});
  const fetch=async(path,request={})=>{
    const body=request.body?JSON.parse(request.body):null;calls.push({path,body,headers:request.headers});
    if(path==='/api/status') return response({csrf_token:++statusGets===1?'old-token':'new-token',llm:{live_enabled:options.liveEnabled!==false},search:{mode:'hybrid',ready:true}});
    if(path==='/api/companies') return response([fixtureProfile('U_1595'),fixtureProfile('U_83932')]);
    if(path.startsWith('/api/news')) return response({as_of:'2026-10-21',items:options.feed||[{event:{...fixtureEvent,...options.event},context_fingerprint:'synthetic-current-context',cached_run_id:options.cachedRunId,business_score:5,relevance_level:'high',reasons:[{text:'Синтетическая причина.'}]}]});
    if(path.startsWith('/api/search')) return options.search?options.search({path,response}):response({items:[]});
    if(path.startsWith('/api/runs/')) return typeof options.saved==='function'?options.saved({path,response}):response(options.saved||fixtureRun());
    if(path.startsWith('/api/jobs/')) return options.job?options.job({path,response}):response({error:'No synthetic job'},404);
    if(path==='/api/feedback') return options.feedback?options.feedback({body,response}):response({saved:true,feedback_id:body.feedback_id});
    if(path==='/api/insights' && body.mode==='sources') return options.sources?options.sources({body,response}):response(fixtureRun('sources_ready',{company_id:body.company_id,role:body.role}));
    if(path==='/api/insights' && body.mode==='live') return options.live?options.live({body,request,response}):response(fixtureRun());
    throw new Error('Unexpected synthetic route: '+path);
  };
  const context=vm.createContext({document,fetch,location:{origin:'http://synthetic.test'},URL,Intl,Date,
    crypto:webcrypto,localStorage:{get length(){return storage.size;},key:index=>[...storage.keys()][index]??null,getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
    setInterval:callback=>{const id=++nextTimer;timers.set(id,callback);return id;},clearInterval:id=>timers.delete(id),
    requestAnimationFrame:callback=>callback(),setTimeout,clearTimeout});
  vm.runInContext(source,context,{filename:'web/app.js'});await flush();await flush();
  return {context,document,calls,timers,storage,response,get:id=>document.getElementById(id),
    livePosts:()=>calls.filter(call=>call.path==='/api/insights' && call.body.mode==='live')};
}

test('pending generation preserves the card, blocks double click, then visibly focuses the complete result',async()=>{
  const wait=deferred();const h=await harness({live:()=>wait.promise});
  await h.context.selectEvent('test-news');const card=h.get('detail').children[0];
  const pending=h.context.selectEvent('test-news','live');
  assert.equal(h.get('detail').children[0],card);
  assert.equal(h.get('generate-insight').disabled,true);
  assert.match(h.get('analysis-feedback').textContent,/Готовим и проверяем инсайт/);
  await h.context.selectEvent('test-news','live');assert.equal(h.livePosts().length,1);
  wait.resolve(h.response(fixtureRun()));await pending;
  assert.match(h.get('analysis-outcome').textContent,/Инсайт готов/);
  assert.equal(h.document.activeElement.id,'analysis-outcome');
  assert.equal(h.document.scrollCalls.at(-1).id,'analysis-outcome');
  assert.equal(h.get('generate-insight').disabled,false);
  assert.equal(h.get('detail').attributes['aria-busy'],'false');assert.equal(h.timers.size,0);
  const requests=h.calls.length;const completeCard=h.get('detail').children[0];
  h.get('generate-insight').listeners.click();
  assert.equal(h.calls.length,requests);assert.equal(h.get('detail').children[0],completeCard);
  assert.match(h.get('generate-insight').textContent,/Показать готовый инсайт/);
});

test('HTTP failure remains next to the action and leaves sources and retry available',async()=>{
  const wait=deferred();let attempts=0;
  const h=await harness({live:({response})=>++attempts===1?response({error:'Синтетическая ошибка хранилища.'},503):wait.promise});
  await h.context.selectEvent('test-news');const card=h.get('detail').children[0];
  await h.context.selectEvent('test-news','live');
  assert.equal(h.get('detail').children[0],card);
  assert.match(h.get('analysis-feedback').textContent,/Синтетическая ошибка хранилища/);
  assert.match(h.get('detail').textContent,/Синтетический документ UI-теста/);
  assert.equal(h.get('analysis-feedback').attributes.role,'alert');
  assert.equal(h.document.activeElement.id,'analysis-feedback');
  assert.equal(h.get('generate-insight').disabled,false);assert.equal(h.livePosts().length,1);assert.equal(h.timers.size,0);
  const retry=h.context.selectEvent('test-news','live');
  assert.equal(h.get('analysis-feedback').attributes.role,'status');
  wait.resolve(h.response(fixtureRun()));await retry;
  assert.match(h.get('analysis-outcome').textContent,/Инсайт готов/);
});

test('a stale action token is refreshed once before the server accepts generation',async()=>{
  let accepted=0;
  const h=await harness({live:({request,response})=>{
    if(request.headers['X-Alfa-Action']==='old-token') return response({error:'Synthetic token rejected before dispatch'},403);
    accepted++;return response(fixtureRun());
  }});
  await h.context.selectEvent('test-news');await h.context.selectEvent('test-news','live');
  assert.equal(h.livePosts().length,2);assert.equal(accepted,1);
  assert.match(h.get('analysis-outcome').textContent,/Инсайт готов/);
});

test('network and invalid-JSON failures are visible and are not automatically retried',async()=>{
  for(const live of [()=>{throw new TypeError('synthetic offline');},()=>({ok:true,status:200,json:async()=>{throw new Error('synthetic private response body');}})]) {
    const h=await harness({live});await h.context.selectEvent('test-news');const card=h.get('detail').children[0];
    await h.context.selectEvent('test-news','live');
    assert.equal(h.get('detail').children[0],card);assert.equal(h.livePosts().length,1);
    assert.match(h.get('analysis-feedback').textContent,/Не удалось завершить запрос/);
    assert.doesNotMatch(h.get('detail').textContent,/synthetic private response body/);
    assert.equal(h.get('generate-insight').disabled,false);assert.equal(h.timers.size,0);
  }
});

test('malformed complete payload cannot erase the existing card or claim success',async()=>{
  const h=await harness({live:({response})=>response(fixtureRun('complete',{insight:null}))});
  await h.context.selectEvent('test-news');const card=h.get('detail').children[0];
  await h.context.selectEvent('test-news','live');
  assert.equal(h.get('detail').children[0],card);
  assert.match(h.get('analysis-feedback').textContent,/неполную карточку/);
  assert.doesNotMatch(h.get('analysis-outcome').textContent,/Инсайт готов/);
});

test('completed main insight survives reload without any new generation',async()=>{
  const storage=new Map();const h=await harness({storage});
  await h.context.selectEvent('test-news');await h.context.selectEvent('test-news','live');
  const reopened=await harness({storage});await reopened.context.selectEvent('test-news');
  assert.equal(reopened.calls.filter(call=>call.path==='/api/insights').length,0);
  assert.match(reopened.get('analysis-outcome').textContent,/Инсайт готов/);
  assert.equal(reopened.document.activeElement.id,'analysis-outcome');
});

test('a current server-side result overrides stale browser storage without generating again',async()=>{
  const h=await harness({cachedRunId:'b'.repeat(32),storage:new Map([['alfa.news.last-run.v1:U_1595:cfo:test-news','c'.repeat(32)]])});
  await h.context.selectEvent('test-news');
  assert.equal(h.calls.filter(call=>call.path==='/api/insights').length,0);
  assert.equal(h.calls.find(call=>call.path.startsWith('/api/runs/')).path,'/api/runs/'+'b'.repeat(32));
  assert.match(h.get('analysis-outcome').textContent,/Открыт сохранённый анализ/);
  assert.match(h.get('detail').textContent,/Открытие сохранённого результата/);
});

test('a ready result is reachable from the top of the page without any generation',async()=>{
  const h=await harness({cachedRunId:'a'.repeat(32)});
  assert.match(h.get('analysis-navigation').textContent,/Основной анализ готов/);
  assert.match(h.get('open-main-analysis').textContent,/Открыть основной анализ/);
  await h.get('open-main-analysis').listeners.click();
  assert.equal(h.calls.filter(call=>call.path==='/api/insights').length,0);
  assert.equal(h.livePosts().length,0);assert.equal(h.document.activeElement.id,'analysis-outcome');
  const before=h.calls.length;
  h.get('open-next-step').listeners.click();
  assert.equal(h.document.activeElement.id,'analysis-next-step');assert.equal(h.calls.length,before);
});

test('the company action precedes the expandable source analysis and source navigation keeps citations',async()=>{
  const h=await harness();await h.context.selectEvent('test-news');await h.context.selectEvent('test-news','live');
  const outcome=h.get('analysis-outcome');
  assert.ok(outcome.children.indexOf(h.get('analysis-next-step'))<outcome.children.indexOf(h.get('analysis-breakdown')));
  assert.equal(h.get('analysis-breakdown').open,false);
  assert.equal(h.get('bank-sources').open,false);
  assert.match(h.get('analysis-breakdown').textContent,/Факт новости.*Позиция Альфа-Банка.*Риски.*Возможности/s);
  assert.match(h.get('bank-sources').textContent,/Синтетический документ UI-теста/);
  const before=h.calls.length;h.get('open-bank-sources').listeners.click();
  assert.equal(h.get('bank-sources').open,true);assert.equal(h.document.activeElement.id,'bank-sources');
  assert.equal(h.calls.length,before);
});

test('a missing-data answer without a ready main result offers a return to the news without generation',async()=>{
  const h=await harness({sources:({body,response})=>response(body.question?fixtureRun('answered',{
    question:body.question,answer_origin:'rules',answer:{title:'Пробелы',summary:'Продажи неизвестны.',items:[],limits:[]}}):fixtureRun('sources_ready'))});
  await h.context.selectEvent('test-news','sources','Каких данных не хватает для полного вывода?','missing_data');
  assert.equal(h.get('open-main-analysis').textContent,'Вернуться к новости');
  assert.equal(h.get('generate-insight').textContent,'Вернуться к новости');
  await h.get('open-main-analysis').listeners.click();
  assert.match(h.get('analysis-outcome').textContent,/Источники готовы/);assert.equal(h.livePosts().length,0);
  assert.equal(h.document.activeElement,h.get('analysis-outcome'));
});

test('saved result with changed news, company, role, date or withdrawn status is not reused',async()=>{
  const key='alfa.news.last-run.v1:U_1595:cfo:test-news';
  for(const saved of [fixtureRun('complete',{event:{...fixtureEvent,content_sha256:'old'}}),fixtureRun('complete',{company_id:'U_83932'}),fixtureRun('complete',{role:'ceo'}),fixtureRun('complete',{analysis_as_of:'2026-10-20'}),fixtureRun('complete',{context_fingerprint:'old-logic-or-bank-context'}),fixtureRun('stale_result'),fixtureRun('insufficient_evidence')]) {
    const h=await harness({storage:new Map([[key,'a'.repeat(32)]]),saved});await h.context.selectEvent('test-news');
    assert.equal(h.calls.filter(call=>call.path==='/api/insights').length,1);
    assert.match(h.get('analysis-outcome').textContent,/Источники готовы/);assert.equal(h.livePosts().length,0);
  }
});

test('a late result cannot overwrite a different company or leave a running progress timer',async()=>{
  const wait=deferred();const h=await harness({live:()=>wait.promise});
  await h.context.selectEvent('test-news');const pending=h.context.selectEvent('test-news','live');
  vm.runInContext("state.company='U_83932'; updateProfile();",h.context);await h.context.loadFeed();
  wait.resolve(h.response(fixtureRun()));await pending;
  assert.doesNotMatch(h.get('detail').textContent,/Инсайт готов/);assert.equal(h.timers.size,0);
  assert.equal(h.get('detail').attributes['aria-busy'],'false');
});

test('evidence refusal and gateway errors are visible outcomes, never completed insights',async()=>{
  for(const status of ['insufficient_evidence','generation_unavailable','validation_failed']) {
    const h=await harness({live:({response})=>response(fixtureRun(status,{error:status==='generation_unavailable'?{message:'Синтетическая ошибка шлюза.'}:undefined}))});
    await h.context.selectEvent('test-news');await h.context.selectEvent('test-news','live');
    assert.equal(h.document.activeElement.id,'analysis-outcome');
    assert.doesNotMatch(h.get('analysis-outcome').textContent,/Инсайт готов/);
    assert.equal(h.get('analysis-outcome').className.includes('error'),true);
    assert.match(h.get('detail').textContent,/Синтетический документ UI-теста/);
  }
});

test('a missing-data question cannot restore the main insight instead of answering',async()=>{
  const h=await harness({sources:({body,response})=>response(body.question?fixtureRun('answered',{
      question:body.question,task:'missing_data',answer_origin:'rules',
      answer:{title:'Каких данных не хватает',summary:'Динамика продаж не установлена.',items:[{text:'Помесячные продажи одежды',purpose:'Проверить применимость общей статистики.'}],limits:['Долг не установлен.']}}):fixtureRun('sources_ready'))});
  await h.context.selectEvent('test-news');await h.context.selectEvent('test-news','live');
  const generated=h.livePosts().length;
  await h.context.selectEvent('test-news','sources','Каких данных не хватает для полного вывода?','missing_data');
  assert.match(h.get('analysis-outcome').textContent,/Ответ на вопрос готов/);
  assert.match(h.get('analysis-outcome').textContent,/Помесячные продажи одежды/);
  assert.match(h.get('analysis-outcome').textContent,/Список составлен по доступным данным компании/);
  assert.equal(h.livePosts().length,generated);
  const posted=h.calls.filter(call=>call.path==='/api/insights').at(-1).body;
  assert.equal(posted.task,'missing_data');assert.match(posted.question,/не хватает/);
  assert.equal(posted.parent_run_id,null);
});

test('business labels are visible while raw feature paths stay in closed details',async()=>{
  const result=fixtureRun();result.insight.actions[0].company_fields=['company.products.credit','company.industry_okved_code'];
  const h=await harness({live:({response})=>response(result)});
  await h.context.selectEvent('test-news');await h.context.selectEvent('test-news','live');
  const visible=node=>node._text+(node.tagName==='details'&&!node.open?node.children.filter(child=>child.tagName==='summary'):node.children).map(visible).join(' ');
  const text=visible(h.get('analysis-outcome'));
  assert.match(text,/В базе отмечен кредитный продукт; текущий долг не установлен/);
  assert.match(text,/Отрасль компании/);
  assert.doesNotMatch(text,/company\.products|company\.industry/);
  assert.match(h.get('analysis-outcome').textContent,/company\.products\.credit/);
});

test('rejected validation has a specific cause and useful next step',async()=>{
  const h=await harness({live:({response})=>response(fixtureRun('validation_failed',{stop_detail:{
    message:'Ответ не прошёл проверку ссылок.',issues:['unknown_or_missing_evidence_id'],next_step:'Проверить фрагмент источника.'}}))});
  await h.context.selectEvent('test-news');await h.context.selectEvent('test-news','live');
  assert.match(h.get('analysis-outcome').textContent,/Ответ не прошёл проверку/);
  assert.match(h.get('analysis-outcome').textContent,/неизвестный или неверный фрагмент/);
  assert.match(h.get('analysis-outcome').textContent,/Следующий шаг/);
  assert.doesNotMatch(h.get('analysis-outcome').textContent,/недостаточно доказательств/);
});

test('switching company clears old feed before the new personalized feed arrives',async()=>{
  const wait=deferred();const h=await harness({cachedRunId:'a'.repeat(32)});
  assert.ok(h.get('open-main-analysis'));
  assert.match(h.get('events').textContent,/Синтетическое событие/);
  const originalFetch=vm.runInContext('fetch',h.context);
  h.context.fetch=(path,options)=>path.startsWith('/api/news')?wait.promise:originalFetch(path,options);
  vm.runInContext("state.company='U_83932';",h.context);const loading=h.context.loadFeed();
  assert.doesNotMatch(h.get('events').textContent,/Синтетическое событие/);
  assert.match(h.get('events').textContent,/Загружаем события/);
  assert.equal(vm.runInContext('state.feed.length',h.context),0);
  assert.equal(h.get('open-main-analysis'),null);
  wait.resolve(h.response({as_of:'2026-10-21',items:[]}));await loading;
  assert.match(h.get('events').textContent,/Снимок пока пуст/);
});

const descendants=node=>[node,...node.children.flatMap(descendants)];

test('compact card keeps dated evidence separate from a conditional interpretation and preserves exact quotes',async()=>{
  const result=fixtureRun();
  result.insight.news_facts[0].text='Синтетический факт, не прогноз.';
  result.insight.bank_positions[0]={...result.insight.bank_positions[0],publication_date:'2026-09-07',quote:'Синтетическая дословная цитата банка.'};
  const h=await harness({saved:result,cachedRunId:'a'.repeat(32)});await h.context.selectEvent('test-news');
  const brief=descendants(h.get('detail')).find(n=>n.className==='evidence-overview');
  assert.match(brief.textContent,/Факт новости.*Синтетический факт, не прогноз.*Датированная позиция банка.*7 сент\. 2026/s);
  assert.match(h.get('analysis-outcome').textContent,/Условная интерпретация.*требует проверки/);
  const quote=descendants(h.get('analysis-breakdown')).find(n=>n.className==='quote-detail');
  assert.equal(Boolean(quote.open),false);assert.match(quote.textContent,/«Синтетическая дословная цитата банка\.»/);
  assert.equal(h.livePosts().length,0);
});

test('empty follow-up gives an inline error and never dispatches a request',async()=>{
  const h=await harness();await h.context.selectEvent('test-news');
  const ask=descendants(h.get('detail')).find(n=>n.tagName==='button'&&n.textContent==='Задать вопрос с проверкой источников');
  const find=descendants(h.get('detail')).find(n=>n.tagName==='button'&&n.textContent==='Найти источники по вопросу');
  const before=h.calls.length;ask.listeners.click();find.listeners.click();
  assert.equal(h.calls.length,before);assert.equal(h.get('question-error').hidden,false);
  assert.match(h.get('question-error').textContent,/Введите вопрос/);
  assert.equal(h.get('followup-question').attributes['aria-invalid'],'true');
  assert.equal(h.document.activeElement.id,'followup-question');
});

test('feed search reports the filtered count and exposes the selected event without relying on color',async()=>{
  const h=await harness();
  assert.match(h.get('feed-caption').textContent,/Показано 1 из 1/);
  h.get('search').value='несуществующий запрос';await h.context.searchFeed();
  assert.match(h.get('feed-caption').textContent,/Показано 0 из 1/);assert.match(h.get('events').textContent,/не найдены/);
  h.get('search').value='';h.get('search').listeners.input();await h.context.selectEvent('test-news');
  const event=h.get('events').children[0];assert.equal(event.attributes['aria-pressed'],'true');
});

test('role change clears a ready card and cannot restore a cached result for a different role',async()=>{
  const h=await harness({cachedRunId:'a'.repeat(32)});await h.context.selectEvent('test-news');
  assert.match(h.get('detail').textContent,/Инсайт готов/);
  h.get('role').value='ceo';h.get('role').listeners.change();
  assert.doesNotMatch(h.get('detail').textContent,/Инсайт готов/);await flush();await flush();
  await h.context.selectEvent('test-news');
  assert.match(h.get('analysis-outcome').textContent,/Источники готовы/);
  assert.equal(h.livePosts().length,0);
});

test('retry after a failed source question preserves the explicit search task',async()=>{
  let attempts=0;
  const h=await harness({sources:({body,response})=>{
    if(body.question&&++attempts===1)throw new TypeError('Synthetic network failure');
    return response(fixtureRun('sources_ready',{question:body.question,task:body.task}));
  }});
  await h.context.selectEvent('test-news');
  await h.context.selectEvent('test-news','sources','инфляция непродовольственные товары','sources');
  const retry=descendants(h.get('analysis-feedback')).find(n=>n.tagName==='button'&&n.textContent==='Повторить запрос');
  assert.ok(retry);await retry.listeners.click();
  const last=h.calls.filter(c=>c.path==='/api/insights').at(-1).body;
  assert.equal(last.task,'sources');assert.equal(last.mode,'sources');assert.equal(last.question,'инфляция непродовольственные товары');
  assert.match(h.get('analysis-outcome').textContent,/Источники готовы/);assert.equal(h.livePosts().length,0);
});

test('sources-only follow-up has one unambiguous search action and sends an explicit task',async()=>{
  const h=await harness({liveEnabled:false});await h.context.selectEvent('test-news');
  const actions=descendants(h.get('detail')).find(n=>n.className==='question-actions');
  assert.equal(actions.children.length,1);assert.equal(actions.children[0].textContent,'Найти источники по вопросу');
  h.get('followup-question').value='Какие источники подтверждают связь с нашей компанией?';
  await actions.children[0].listeners.click();await flush();await flush();
  const posted=h.calls.filter(c=>c.path==='/api/insights').at(-1).body;
  assert.equal(posted.mode,'sources');assert.equal(posted.task,'sources');
  assert.equal(posted.question,'Какие источники подтверждают связь с нашей компанией?');
  assert.equal(h.livePosts().length,0);
});

test('the live follow-up preserves intent routing and uses a neutral example',async()=>{
  const h=await harness();await h.context.selectEvent('test-news');
  const ask=descendants(h.get('detail')).find(n=>n.tagName==='button'&&n.textContent==='Задать вопрос с проверкой источников');
  assert.doesNotMatch(h.get('followup-question').placeholder,/одежд|автомобил/);
  h.get('followup-question').value='Каких данных не хватает для строительных проектов?';
  await ask.listeners.click();await flush();await flush();
  const posted=h.calls.filter(c=>c.path==='/api/insights').at(-1).body;
  assert.equal(posted.mode,'live');assert.equal(posted.task,'missing_data');
  assert.equal(posted.question,'Каких данных не хватает для строительных проектов?');
});

test('explanation retry retains its main result while a role change drops that parent',async()=>{
  let failed=false;
  const h=await harness({live:({body,response})=>{
    if(body.question&&!failed){failed=true;throw new TypeError('Synthetic explanation request failure');}
    return response(fixtureRun('complete',{question:body.question,company_id:body.company_id,role:body.role}));
  }});
  await h.context.selectEvent('test-news');await h.context.selectEvent('test-news','live');
  await h.context.selectEvent('test-news','live','Почему это важно для компании?');
  const retry=descendants(h.get('analysis-feedback')).find(n=>n.tagName==='button'&&n.textContent==='Повторить запрос');
  await retry.listeners.click();
  const attempts=h.livePosts().filter(c=>c.body.question);
  assert.equal(attempts.length,2);
  for(const {body} of attempts) assert.equal(body.parent_run_id,'a'.repeat(32));
  h.get('role').value='ceo';h.get('role').listeners.change();await flush();await flush();
  await h.context.selectEvent('test-news');await h.context.selectEvent('test-news','live','Какие риски приоритетны для руководителя?');
  const posted=h.livePosts().at(-1).body;
  assert.equal(posted.role,'ceo');assert.equal(posted.parent_run_id,null);
});

test('a failed old feed request cannot put an error over the new company context',async()=>{
  const h=await harness(),wait=deferred();
  const originalFetch=vm.runInContext('fetch',h.context);let delay=true;
  h.context.fetch=(path,options)=>path.startsWith('/api/news')&&delay?(delay=false,wait.promise):originalFetch(path,options);
  const oldFeed=h.context.loadFeed();
  vm.runInContext("state.company='U_83932';",h.context);await h.context.loadFeed();
  wait.resolve(h.response({error:'Ошибка уже покинутой компании.'},503));await oldFeed;
  assert.doesNotMatch(h.get('notice').textContent,/Ошибка уже покинутой компании/);
  assert.match(h.get('events').textContent,/Синтетическое событие/);
});

const searchRow=(title=fixtureEvent.title)=>({event:{...fixtureEvent,title},business_score:5,relevance_level:'high',reasons:[{text:'Синтетическая причина.'}]});

test('feed uses semantic server ranking even when the query has no words from the headline',async()=>{
  const h=await harness({search:({response})=>response({items:[searchRow()]})});
  h.get('search').value='Работодатели повышают оплату персонала';await h.context.searchFeed();
  assert.match(h.get('events').textContent,/Синтетическое событие/);
  assert.match(h.get('feed-caption').textContent,/Показано 1 из 1/);
  assert.match(h.get('search-status').textContent,/Смысловой поиск/);
  assert.equal(h.livePosts().length,0);
});

test('hybrid exact-text result below the dense cutoff remains visible in the feed',async()=>{
  const row={...searchRow('Точное совпадение из полного текста'),search_match:{semantic_score:.61,exact_match:true,retrieval_method:'hybrid'}};
  const h=await harness({search:({response})=>response({items:[row]})});
  h.get('search').value='точная фраза';await h.context.searchFeed();
  assert.match(h.get('events').textContent,/Точное совпадение из полного текста/);
  assert.equal(h.livePosts().length,0);
});

test('a newer semantic query wins when responses arrive in reverse order',async()=>{
  const first=deferred(),second=deferred();let count=0;
  const h=await harness({search:()=>++count===1?first.promise:second.promise});
  h.get('search').value='Первый запрос';const old=h.context.searchFeed();
  h.get('search').value='Второй запрос';const current=h.context.searchFeed();
  second.resolve(h.response({items:[searchRow('Новый результат')]}));await current;
  first.resolve(h.response({items:[searchRow('Старый результат')]}));await old;
  assert.match(h.get('events').textContent,/Новый результат/);
  assert.doesNotMatch(h.get('events').textContent,/Старый результат/);
});

test('late search cannot replace the feed after a company change',async()=>{
  const wait=deferred();const h=await harness({search:()=>wait.promise});
  h.get('search').value='Расходы покупателей';const pending=h.context.searchFeed();
  vm.runInContext("state.company='U_83932';",h.context);await h.context.loadFeed();
  wait.resolve(h.response({items:[searchRow('Устаревший поиск')]}));await pending;
  assert.equal(h.get('search').value,'');
  assert.doesNotMatch(h.get('events').textContent,/Устаревший поиск/);
  assert.match(h.get('events').textContent,/Синтетическое событие/);
});

test('typing is debounced and clearing search immediately restores the feed',async()=>{
  const h=await harness();
  for(const value of ['р','ро','рост цен']) {h.get('search').value=value;h.get('search').listeners.input();}
  await new Promise(done=>setTimeout(done,320));
  const queries=h.calls.filter(call=>call.path.startsWith('/api/search'));
  assert.equal(queries.length,1);assert.equal(new URL(queries[0].path,'http://test').searchParams.get('q'),'рост цен');
  h.get('search').value='';h.get('search').listeners.input();
  assert.match(h.get('events').textContent,/Синтетическое событие/);
});

test('search error and retry preserve an open analysis and make no LLM calls',async()=>{
  let attempts=0;
  const h=await harness({search:({response})=>++attempts===1?response({error:'Поиск временно недоступен.'},503):response({items:[searchRow()]})});
  await h.context.selectEvent('test-news');const card=h.get('detail').children[0];
  h.get('search').value='Произвольный вопрос';await h.context.searchFeed();
  assert.match(h.get('feed-caption').textContent,/Поиск временно недоступен/);
  assert.equal(h.get('detail').children[0],card);
  const retry=h.get('events').children[0];assert.equal(retry.textContent,'Повторить поиск');
  await retry.listeners.click();
  assert.match(h.get('events').textContent,/Синтетическое событие/);
  assert.equal(h.get('detail').children[0],card);assert.equal(h.livePosts().length,0);
});

test('opening analysis while search is pending does not leave the feed permanently busy',async()=>{
  const wait=deferred();const h=await harness({search:()=>wait.promise});
  h.get('search').value='Смысловой запрос';const pending=h.context.searchFeed();
  await h.context.selectEvent('test-news');
  wait.resolve(h.response({items:[searchRow()]}));await pending;
  assert.match(h.get('feed-caption').textContent,/Показано 1 из 1/);
  assert.match(h.get('events').textContent,/Синтетическое событие/);
  assert.match(h.get('analysis-outcome').textContent,/Источники готовы/);
});

const mainJobContext={company_id:'U_1595',role:'cfo',event_id:'test-news',as_of:'2026-10-21',mode:'live',task:'insight',question:'',parent_run_id:null};
const jobId='d'.repeat(32),pendingKey='alfa.news.pending.v2:'+JSON.stringify(mainJobContext);
const jobFixture=(state='done',extra={})=>({job_id:jobId,state,context:{...mainJobContext},progress:{stage:'review',message:'Проверяем обоснованность вывода'},run:state==='done'?fixtureRun():undefined,...extra});

test('explicit retry of a failed job creates a fresh logical id',async()=>{
  let posts=0;
  const h=await harness({live:({body,response})=>response(++posts===1?
    jobFixture('failed',{context:{...body},error:{message:'Synthetic terminal failure'}}):fixtureRun())});
  await h.context.selectEvent('test-news');await h.context.selectEvent('test-news','live');
  const retry=descendants(h.get('analysis-feedback')).find(n=>n.tagName==='button'&&n.textContent==='Повторить запрос');
  assert.ok(retry);await retry.listeners.click();
  assert.equal(h.livePosts().length,2);
  assert.notEqual(h.livePosts()[0].body.request_id,h.livePosts()[1].body.request_id);
  assert.match(h.get('analysis-outcome').textContent,/Инсайт готов/);
});

test('retry after an uncertain POST preserves its logical id',async()=>{
  let posts=0;
  const h=await harness({live:({response})=>{if(++posts===1)throw new TypeError('Lost response after acceptance');return response(fixtureRun());}});
  await h.context.selectEvent('test-news');await h.context.selectEvent('test-news','live');
  const retry=descendants(h.get('analysis-feedback')).find(n=>n.tagName==='button'&&n.textContent==='Повторить запрос');
  await retry.listeners.click();
  assert.equal(h.livePosts().length,2);
  assert.equal(h.livePosts()[0].body.request_id,h.livePosts()[1].body.request_id);
});

test('reload after a lost POST response preserves the pending logical attempt',async()=>{
  const storage=new Map();
  const first=await harness({storage,live:()=>{throw new TypeError('Synthetic response lost');}});
  await first.context.selectEvent('test-news');await first.context.selectEvent('test-news','live');
  const reopened=await harness({storage});
  assert.equal(reopened.livePosts().length,0);
  await reopened.context.selectEvent('test-news','live');
  assert.equal(reopened.livePosts()[0].body.request_id,first.livePosts()[0].body.request_id);
});

test('explicit regeneration during active work has an independent pending key',async()=>{
  const waits=[deferred(),deferred()];let index=0;
  const h=await harness({live:()=>waits[index++].promise});
  await h.context.selectEvent('test-news');
  const first=h.context.selectEvent('test-news','live');await flush();
  const second=h.context.selectEvent('test-news','live','',undefined,{regenerate:true});await flush();
  const count=h.livePosts().length;
  waits[0].resolve(h.response(fixtureRun('complete',{run_id:'1'.repeat(32)})));
  waits[1].resolve(h.response(fixtureRun('complete',{run_id:'2'.repeat(32)})));
  await Promise.all([first,second]);
  assert.equal(count,2);assert.notEqual(h.livePosts()[0].body.request_id,h.livePosts()[1].body.request_id);
  assert.equal(h.get('analysis-outcome')!==null,true);
});

test('reload restores a specific regeneration without posting or consuming another pending attempt',async()=>{
  const gate=deferred(),jobs=new Map();let index=0;
  const storage=new Map();
  const h=await harness({storage,live:({body,response})=>{
    const id=(++index===1?'1':'2').repeat(32);
    const job=jobFixture('queued',{job_id:id,context:{...body}});jobs.set(id,job);return response(job);
  },job:async({path,response})=>{await gate.promise;return response({...jobs.get(path.split('/').at(-1)),state:'done',run:fixtureRun()});}});
  await h.context.selectEvent('test-news');
  const ordinary=h.context.selectEvent('test-news','live');await flush();
  const regenerated=h.context.selectEvent('test-news','live','',undefined,{regenerate:true});await flush();
  const pendingKeys=[...storage.keys()].filter(key=>key.startsWith('alfa.news.pending.v2:'));
  assert.equal(pendingKeys.length,2);
  const reopened=await harness({storage,job:({path,response})=>response({...jobs.get(path.split('/').at(-1)),state:'done',run:fixtureRun()})});
  await reopened.context.selectEvent('test-news');
  const buttons=descendants(reopened.get('article-background-tasks')).filter(n=>n.tagName==='button');
  assert.equal(buttons.length,2);
  await buttons.find(n=>n.textContent.includes('перегенерация')).listeners.click();
  assert.equal(reopened.livePosts().length,0);
  assert.equal([...storage.keys()].filter(key=>key.startsWith('alfa.news.pending.v2:')).length,1);
  gate.resolve();await Promise.all([ordinary,regenerated]);
});

test('asynchronous generation polls progress and creates exactly one paid-action request',async()=>{
  let polls=0;
  const h=await harness({live:({response})=>response(jobFixture('queued')),job:({response})=>response(++polls===1?jobFixture('running'):jobFixture())});
  await h.context.selectEvent('test-news');const card=h.get('detail').children[0];
  const pending=h.context.selectEvent('test-news','live');await flush();await flush();
  assert.equal(h.get('detail').children[0],card);assert.equal(h.storage.get(pendingKey),jobId);
  assert.match(h.get('job-stage').textContent,/в очереди/);
  await pending;
  assert.match(h.get('analysis-outcome').textContent,/Инсайт готов/);
  assert.equal(h.livePosts().length,1);assert.equal(polls,2);assert.equal(h.storage.has(pendingKey),false);
});

test('a reload restores a completed background task only through GET without regenerating',async()=>{
  const h=await harness({storage:new Map([[pendingKey,jobId]]),job:({response})=>response(jobFixture())});
  await h.context.selectEvent('test-news','live');
  assert.match(h.get('analysis-outcome').textContent,/Инсайт готов/);
  assert.match(h.get('analysis-outcome').textContent,/Открыт сохранённый анализ/);
  assert.equal(h.calls.filter(call=>call.path==='/api/insights').length,0);
  assert.equal(h.storage.has(pendingKey),false);
});

test('a network failure while restoring a task keeps its id and cannot start a replacement generation',async()=>{
  const h=await harness({storage:new Map([[pendingKey,jobId]]),job:()=>{throw new TypeError('Synthetic offline');}});
  await h.context.selectEvent('test-news','live');
  assert.equal(h.calls.filter(call=>call.path==='/api/insights').length,0);
  assert.equal(h.storage.get(pendingKey),jobId);
  assert.match(h.get('analysis-feedback').textContent,/Нет соединения/);
});

test('an interrupted terminal task clears its pending pointer and offers an explicit retry',async()=>{
  const h=await harness({storage:new Map([[pendingKey,jobId]]),job:({response})=>response(jobFixture('failed',{error:{message:'Сервер был перезапущен.'}}))});
  await h.context.selectEvent('test-news','live');
  assert.match(h.get('analysis-feedback').textContent,/Сервер был перезапущен/);
  assert.equal(h.storage.has(pendingKey),false);assert.equal(h.livePosts().length,0);
});

test('real feedback sends one stable id and does not call an LLM',async()=>{
  const h=await harness({cachedRunId:'a'.repeat(32)});await h.context.selectEvent('test-news');
  const controls=descendants(h.get('feedback-block')).filter(control=>control.dataset.rating);
  await controls.find(control=>control.dataset.rating==='helpful').listeners.click();
  await controls.find(control=>control.dataset.rating==='incorrect').listeners.click();
  const feedback=h.calls.filter(call=>call.path==='/api/feedback');
  assert.equal(feedback.length,2);assert.equal(feedback[0].body.feedback_id,feedback[1].body.feedback_id);
  assert.match(feedback[0].body.feedback_id,/^[a-f0-9]{32}$/);
  assert.equal(feedback[1].body.run_id,'a'.repeat(32));assert.equal(feedback[1].body.rating,'incorrect');
  assert.match(h.get('feedback-status').textContent,/оценка сохранена/);assert.equal(h.livePosts().length,0);
});

test('reload preserves the selected company and role and ignores unavailable saved contexts',async()=>{
  const first=await harness();
  first.get('company').value='U_83932';first.get('company').listeners.change();
  first.get('role').value='ceo';first.get('role').listeners.change();await flush();
  const reloaded=await harness({storage:first.storage});
  assert.equal(reloaded.get('company').value,'U_83932');assert.equal(reloaded.get('role').value,'ceo');
  assert.match(reloaded.calls.find(call=>call.path.startsWith('/api/news')).path,/company_id=U_83932&role=ceo/);
  assert.equal(reloaded.livePosts().length,0);
  const invalid=await harness({storage:new Map([['alfa.news.context.v1',JSON.stringify({company_id:'missing',role:'invalid'})]])});
  assert.equal(invalid.get('company').value,'U_1595');assert.equal(invalid.get('role').value,'cfo');
});


test('two article analyses run independently and a late result cannot replace the selected article',async()=>{
  const first=deferred(),second=deferred();
  const h=await harness({feed:multiFeed,sources:({body,response})=>response(runFor(body.event_id,'sources_ready')),
    live:({body})=>body.event_id==='test-news'?first.promise:second.promise,
    saved:({path,response})=>response(runFor(path.endsWith('a'.repeat(32))?'test-news':'other-news'))});
  await h.context.selectEvent('test-news');const a=h.context.selectEvent('test-news','live');await flush();
  await h.context.selectEvent('other-news');assert.equal(h.get('generate-insight').disabled,false);
  const b=h.context.selectEvent('other-news','live');await flush();assert.equal(h.livePosts().length,2);
  second.resolve(h.response(runFor('other-news')));await b;
  const card=h.get('detail').children[0],focus=h.document.focusCalls.length;
  first.resolve(h.response(runFor('test-news')));await a;
  assert.equal(h.get('detail').children[0],card);assert.equal(h.document.focusCalls.length,focus);
  assert.match(h.get('detail').textContent,/Другая синтетическая статья/);
  await h.context.selectEvent('test-news');assert.equal(h.livePosts().length,2);
  assert.match(h.get('analysis-outcome').textContent,/Инсайт готов/);
});

test('returning to an article still being generated joins its request without a second generation',async()=>{
  const wait=deferred();const h=await harness({feed:multiFeed,
    sources:({body,response})=>response(runFor(body.event_id,'sources_ready')),live:()=>wait.promise});
  await h.context.selectEvent('test-news');const first=h.context.selectEvent('test-news','live');await flush();
  await h.context.selectEvent('other-news');const returned=h.context.selectEvent('test-news','live');await flush();
  assert.equal(h.livePosts().length,1);assert.equal(h.get('generate-insight').disabled,true);
  wait.resolve(h.response(runFor('test-news')));await Promise.all([first,returned]);
  assert.match(h.get('analysis-outcome').textContent,/Инсайт готов/);assert.equal(h.timers.size,0);
});

test('the last failed attempt is restored with plain-language reasons and explicit fresh regeneration',async()=>{
  const failed=runFor('test-news','validation_failed',{llm_request_attempted:true,stop_detail:{message:'Ответ не опубликован.',
    issues:['credit_cost_requires_contract_condition','forecast_not_supported_by_quote']}});
  const storage=new Map([['alfa.news.last-attempt.v1:U_1595:cfo:test-news','a'.repeat(32)]]);
  const h=await harness({storage,saved:failed,live:({response})=>response(runFor('test-news'))});
  await h.context.selectEvent('test-news');
  assert.equal(h.livePosts().length,0);assert.equal(h.get('analysis-outcome').attributes.role,'alert');
  assert.match(h.get('analysis-outcome').textContent,/условиями договора/);assert.match(h.get('analysis-outcome').textContent,/Цитата не подтверждает прогноз/);
  assert.doesNotMatch(h.get('analysis-outcome').textContent,/credit_cost|forecast_not_supported/);
  assert.equal(h.get('analysis-trace').open,false);assert.equal(h.get('generate-insight').textContent,'Перегенерировать');
  await h.get('generate-insight').listeners.click();
  assert.equal(h.livePosts().length,1);assert.equal(h.livePosts()[0].body.regenerate,true);
  assert.match(h.livePosts()[0].body.request_id,/^[a-f0-9]{32}$/);
  assert.match(h.get('analysis-outcome').textContent,/Инсайт готов/);
});

test('regenerating an existing success uses a new request id and blocks only its duplicate click',async()=>{
  const wait=deferred();let count=0;
  const h=await harness({live:({response})=>++count===1?response(fixtureRun()):wait.promise});
  await h.context.selectEvent('test-news');await h.context.selectEvent('test-news','live');
  const button=h.get('regenerate-insight');const retry=button.listeners.click();await flush();
  await button.listeners.click();assert.equal(h.livePosts().length,2);
  assert.equal(h.livePosts()[1].body.regenerate,true);
  assert.notEqual(h.livePosts()[0].body.request_id,h.livePosts()[1].body.request_id);
  wait.resolve(h.response(fixtureRun('complete',{run_id:'c'.repeat(32)})));await retry;
  assert.equal(h.get('regenerate-insight').disabled,false);
});

test('a background article failure is retained without interrupting the current article',async()=>{
  const wait=deferred();const failed=runFor('test-news','validation_failed',{llm_request_attempted:true,
    stop_detail:{message:'Проверка не пройдена.',issues:['forecast_not_supported_by_quote']}});
  const h=await harness({feed:multiFeed,sources:({body,response})=>response(runFor(body.event_id,'sources_ready')),
    live:()=>wait.promise,saved:failed});
  await h.context.selectEvent('test-news');const a=h.context.selectEvent('test-news','live');await flush();
  await h.context.selectEvent('other-news');const card=h.get('detail').children[0];
  wait.resolve(h.response(failed));await a;
  assert.equal(h.get('detail').children[0],card);assert.match(h.get('events').textContent,/Анализ не прошёл проверку/);
  await h.context.selectEvent('test-news');assert.equal(h.get('generate-insight').textContent,'Перегенерировать');
  assert.equal(h.livePosts().length,1);
});

test('a persisted job is polled to completion after navigating to another article',async()=>{
  const done=deferred(),jobId='f'.repeat(32);let polls=0;
  const h=await harness({feed:multiFeed,sources:({body,response})=>response(runFor(body.event_id,'sources_ready')),
    live:({response})=>response({job_id:jobId,state:'running'}),job:()=>{polls++;return done.promise;}});
  await h.context.selectEvent('test-news');const a=h.context.selectEvent('test-news','live');await flush();
  await h.context.selectEvent('other-news');
  await new Promise(resolve=>setTimeout(resolve,1050));
  done.resolve(h.response({job_id:jobId,state:'done',run:runFor('test-news')}));await a;
  assert.ok(polls>0);assert.equal(h.livePosts().length,1);
  assert.equal(h.storage.get('alfa.news.last-run.v1:U_1595:cfo:test-news'),'a'.repeat(32));
  assert.match(h.get('detail').textContent,/Другая синтетическая статья/);
});

const questionJobContext=(question,extra={})=>({...mainJobContext,task:'explain',question,...extra});
const pendingKeyFor=context=>'alfa.news.pending.v2:'+JSON.stringify(context);
const questionRun=question=>fixtureRun('answered',{question,task:'explain',answer_origin:'llm',
  answer:{title:question,summary:'Синтетический ответ только на этот вопрос.',points:[],uncertainties:[]}});

test('different questions in the same article run concurrently and cannot replace each other',async()=>{
  const first=deferred(),second=deferred();
  const h=await harness({live:({body})=>body.question==='Первый вопрос?'?first.promise:second.promise});
  await h.context.selectEvent('test-news');
  const a=h.context.selectEvent('test-news','live','Первый вопрос?');await flush();
  const b=h.context.selectEvent('test-news','live','Второй вопрос?');await flush();
  assert.equal(h.livePosts().length,2);
  assert.equal(h.livePosts()[0].body.task,'explain');
  second.resolve(h.response(questionRun('Второй вопрос?')));await b;
  assert.match(h.get('analysis-outcome').textContent,/Второй вопрос/);
  assert.match(h.get('events').textContent,/Анализ выполняется/);
  first.resolve(h.response(questionRun('Первый вопрос?')));await a;
  assert.match(h.get('analysis-outcome').textContent,/Второй вопрос/);
  assert.doesNotMatch(h.get('analysis-outcome').textContent,/Первый вопрос/);
  assert.doesNotMatch(h.get('events').textContent,/Анализ выполняется/);
});

test('same question in sources mode never joins its running live job',async()=>{
  const wait=deferred();const h=await harness({live:()=>wait.promise});
  await h.context.selectEvent('test-news');
  const live=h.context.selectEvent('test-news','live','Проверить спрос?');await flush();
  await h.context.selectEvent('test-news','sources','Проверить спрос?','sources');
  assert.equal(h.calls.filter(call=>call.path==='/api/insights'&&call.body.question==='Проверить спрос?').length,2);
  assert.match(h.get('analysis-outcome').textContent,/Источники готовы/);
  wait.resolve(h.response(questionRun('Проверить спрос?')));await live;
  assert.match(h.get('analysis-outcome').textContent,/Источники готовы/);
});

test('persisted jobs require exact mode task full question date company role and parent',async()=>{
  const expected=questionJobContext('Полный вопрос?');const key=pendingKeyFor(expected);
  const variants=[{mode:'sources'},{task:'sources'},{question:'Другой вопрос?'},{as_of:'2026-10-20'},
    {company_id:'U_83932'},{role:'ceo'},{parent_run_id:'b'.repeat(32)}];
  for(const variant of variants) {
    const h=await harness({storage:new Map([[key,jobId]]),job:({response})=>response(jobFixture('done',{
      context:{...expected,...variant},run:questionRun('Чужой ответ')}))});
    await h.context.selectEvent('test-news','live','Полный вопрос?');
    assert.equal(h.livePosts().length,0);
    assert.match(h.get('analysis-feedback').textContent,/другому вопросу или контексту/);
    assert.doesNotMatch(h.get('detail').textContent,/Чужой ответ/);
  }
});

test('two different question jobs survive reload and resume only their exact contexts',async()=>{
  const first=questionJobContext('Первый вопрос?'),second=questionJobContext('Второй вопрос?');
  const firstId='a'.repeat(32),secondId='b'.repeat(32);
  const storage=new Map([[pendingKeyFor(first),firstId],[pendingKeyFor(second),secondId]]);
  const job=({path,response})=>{const context=path.endsWith(firstId)?first:second;
    return response(jobFixture('done',{job_id:path.split('/').at(-1),context,run:questionRun(context.question)}));};
  const h=await harness({storage,job});await h.context.selectEvent('test-news');
  assert.match(h.get('article-background-tasks').textContent,/Первый вопрос/);
  assert.match(h.get('article-background-tasks').textContent,/Второй вопрос/);
  assert.equal(h.livePosts().length,0);
  await h.context.selectEvent('test-news','live',first.question);
  assert.match(h.get('analysis-outcome').textContent,/Первый вопрос/);
  assert.equal(storage.get(pendingKeyFor(second)),secondId);
  const reloaded=await harness({storage,job});
  await reloaded.context.selectEvent('test-news','live',second.question);
  assert.match(reloaded.get('analysis-outcome').textContent,/Второй вопрос/);
  assert.equal(reloaded.livePosts().length,0);assert.equal(storage.size,0);
});

test('an older completed job cannot erase a newer pointer for the same full context',async()=>{
  const done=deferred(),key=pendingKeyFor(mainJobContext),newId='e'.repeat(32);
  const h=await harness({live:({response})=>response(jobFixture('running')),job:()=>done.promise});
  await h.context.selectEvent('test-news');
  const old=h.context.selectEvent('test-news','live');await flush();
  h.storage.set(key,newId);
  done.resolve(h.response(jobFixture('done')));await old;
  assert.equal(h.storage.get(key),newId);
});

test('progress identifies the full question and different tasks have different keys',async()=>{
  const done=deferred(),context=questionJobContext('Как проверить условия договора?');
  const h=await harness({live:({response})=>response(jobFixture('running',{context})),job:()=>done.promise});
  await h.context.selectEvent('test-news');const pending=h.context.selectEvent('test-news','live',context.question);await flush();
  assert.match(h.get('job-stage').textContent,/Как проверить условия договора/);
  assert.notEqual(h.context.jobKey(context),h.context.jobKey({...context,task:'sources'}));
  assert.notEqual(h.context.jobKey(context),h.context.jobKey({...context,parent_run_id:'c'.repeat(32)}));
  assert.equal(h.context.jobKey({...context,task:'auto'}),h.context.jobKey(context));
  done.resolve(h.response(jobFixture('done',{context,run:questionRun(context.question)})));await pending;
});

test('uncertain paid request warning is visible before explicit regeneration',async()=>{
  const message='Запрос мог быть обработан и оплачен. Новая попытка может повторно расходовать бюджет.';
  const h=await harness({live:({response})=>response(fixtureRun('generation_unavailable',{
    llm_request_attempted:true,error:{code:'request_outcome_unknown',message},stop_detail:{message}}))});
  await h.context.selectEvent('test-news');await h.context.selectEvent('test-news','live');
  assert.match(h.get('analysis-outcome').textContent,/оплачен/);
  assert.match(h.get('analysis-outcome').textContent,/повторно расходовать бюджет/);
  assert.equal(h.get('generate-insight').textContent,'Перегенерировать');
  assert.equal(h.livePosts().length,1);
});

test('queue overflow stays beside the action and does not submit a second analysis',async()=>{
  const h=await harness({live:({response})=>response({error:'Очередь анализа заполнена. Дождитесь завершения текущих заданий.',code:'queue_full'},503)});
  await h.context.selectEvent('test-news');const card=h.get('detail').children[0];
  await h.context.selectEvent('test-news','live');
  assert.match(h.get('analysis-feedback').textContent,/Очередь анализа заполнена/);
  assert.equal(h.get('detail').children[0],card);assert.equal(h.livePosts().length,1);
  assert.equal(h.get('generate-insight').disabled,false);
});

test('incomplete long-article context is disclosed in the result',async()=>{
  const h=await harness({live:({response})=>response(fixtureRun('complete',{news_context:{context_incomplete:true,
    context_warning:'Для анализа выбраны фрагменты из всей статьи. Полнота контекста не подтверждена.'}}))});
  await h.context.selectEvent('test-news');await h.context.selectEvent('test-news','live');
  assert.match(h.get('detail').textContent,/выбраны фрагменты из всей статьи/);
  assert.match(h.get('detail').textContent,/Полнота контекста не подтверждена/);
});

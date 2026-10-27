"""Scope, question routing and cache regressions; all transports are synthetic."""
import copy
from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from alfa_app.agent import Agent,check_draft,check_answer,context_fingerprint,literal_quotes,plan_queries
from alfa_app.config import ROOT,Settings
from alfa_app.evidence import attach_page_context,model_evidence
from alfa_app.server import Application
from alfa_app.store import Store
from alfa_app.tasks import task_for
from alfa_research import retrieve
from test_service import valid_draft


class SemanticTests(unittest.TestCase):
    def setUp(self):
        base=ROOT/'.cache/tests';base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base)
        self.settings=Settings(api_key='synthetic-only',allow_live=True,runtime_dir=Path(self.temp.name))
        self.store=Store(self.settings.runtime_dir/'service.sqlite')
        self.profile=json.loads((ROOT/'data/company_U_1595.json').read_text(encoding='utf-8'))
        self.event=next(json.loads(line) for line in (ROOT/'data/news_snapshot.jsonl').read_text(encoding='utf-8').splitlines()
                        if json.loads(line)['event_id']=='news_9f10b60aed544eaa32d4')
        self.as_of=date(2026,10,21)
        self.sources=attach_page_context(self.settings.research_database,retrieve(self.settings.research_database,
            'Оборот розничной торговли июле замедление потребления',self.as_of,limit=1,source_id='alfa_labor_7m'))

    def tearDown(self):self.temp.cleanup()

    def draft(self):
        draft=valid_draft({'bank_evidence':self.sources,'news':self.event})
        source=self.sources[0]
        quote=re.search(r'Основной причиной.*?автомобилей\.',source['page_context'],re.S)[0]
        self.assertIn(quote,source['text'])
        draft['bank_positions'][0].update(text='2026-09-07: банк связывает замедление непродовольственной розницы в июле, в частности, с автомобилями.',quote=quote)
        draft['title']='Что проверить продавцу одежды по данным о рознице'
        draft['scope_note']='Банк рассматривает непродовольственную розницу, в частности автомобили. Динамика одежды и продаж компании не установлена; нужны данные клиента.'
        draft['interpretations'][0]['text']='Применимость наблюдения к одежде не установлена; нужно проверить помесячные продажи клиента.'
        return draft

    def check(self,draft):return check_draft(draft,self.profile,self.event,self.sources,self.settings.research_database)

    def test_exact_old_style_quote_does_not_establish_clothing_demand(self):
        draft=self.draft();draft['title']='Торговля одеждой: охлаждение спроса'
        draft.pop('scope_note');draft['bank_positions'][0]['text']='2026-09-07: банк оценивает замедление роста оборота розницы как охлаждение непродовольственного спроса.'
        draft['interpretations'][0]['text']='Если непродовольственный спрос охлаждается, выручка продавца одежды может расти медленнее.'
        issues=self.check(draft)
        for code in ['bank_segment_qualifier_missing','title_overstates_clothing_evidence','clothing_summary_overstates_macro_evidence','cross_segment_scope_not_explained']:
            self.assertIn(code,issues)

    def test_scoped_statement_retains_automobiles_period_and_limits(self):
        self.assertEqual(self.check(self.draft()),[])

    def test_explicit_missing_confirmation_phrase_is_not_a_scope_failure(self):
        draft=self.draft();draft['scope_note']='Автомобили — исходный пример; перенос на одежду возможен после проверки продаж. Подтверждения спроса именно на одежду у клиента нет.'
        self.assertNotIn('cross_segment_scope_not_explained',self.check(draft))

    def test_structured_publication_date_is_verified_and_not_confused_with_observation_period(self):
        draft=self.draft();position=draft['bank_positions'][0]
        position['text']=position['text'].replace('2026-09-07: ','')
        position['publication_date']='2026-09-07'
        self.assertEqual(self.check(draft),[])
        position['publication_date']='2026-07-01'
        self.assertIn('bank_publication_date_mismatch',self.check(draft))

    def test_interrogative_title_does_not_assert_clothing_slowdown(self):
        draft=self.draft();draft['title']='Замедление непродовольственного спроса: затрагивает ли оно продажи одежды клиента?'
        self.assertNotIn('title_overstates_clothing_evidence',self.check(draft))

    def test_conditional_macro_event_does_not_establish_existing_financing(self):
        draft=self.draft();draft['risks'][0]['text']='Если спрос ослабнет, это осложнит обслуживание имеющегося финансирования.'
        self.assertIn('active_debt_not_established',self.check(draft))

    def test_negated_forecast_and_verified_short_date_do_not_reject_observation(self):
        draft=self.draft();position=draft['bank_positions'][0]
        position.update(text='Позиция банка от 7 сентября: замедление непродовольственной розницы в июле связано в частности с автомобилями; это оценка, а не прогноз.',publication_date='2026-09-07')
        self.assertEqual(self.check(draft),[])
        position['text']=position['text'].replace('7 сентября','8 сентября')
        self.assertIn('unreviewed_bank_numeric_claim',self.check(draft))

    def test_historical_candidate_requires_new_checks_and_new_review(self):
        calls=[]
        candidate=Agent(self.settings,self.store,None).run(self.profile,self.event,self.as_of)
        candidate.update(status='complete',insight=self.draft(),context_fingerprint='old-logic')
        class Fake:
            def generate(inner,messages,identity,stage,max_tokens=None):
                calls.append(stage);self.assertEqual(stage,'review')
                return {'result':{'approved':True,'issues':[],'additional_queries':[]},'cached':False}
        run=Agent(self.settings,self.store,Fake()).run(self.profile,self.event,self.as_of,live=True,candidate_run=candidate)
        self.assertEqual(run['status'],'complete');self.assertEqual(calls,['review'])
        self.assertIn('candidate_recheck',[step['step'] for step in run['trace']])
        self.assertNotEqual(run['context_fingerprint'],'old-logic')

    def test_wrong_company_or_invalid_candidate_cannot_reuse_approval(self):
        candidate=Agent(self.settings,self.store,None).run(self.profile,self.event,self.as_of)
        candidate.update(status='complete',insight=self.draft())
        candidate['insight']['title']='Одежда: охлаждение спроса'
        class Never:
            def generate(self,*args,**kwargs):raise AssertionError('Invalid candidate cannot reach review')
        one=replace(self.settings,max_rounds=1)
        run=Agent(one,self.store,Never()).run(self.profile,self.event,self.as_of,live=True,candidate_run=candidate)
        self.assertEqual(run['status'],'validation_failed');self.assertIsNone(run['insight'])

    def test_missing_parent_id_does_not_break_source_search(self):
        app=Application(self.settings,seed=False);app.store.put_event(self.event)
        run=app.dispatch_post('/api/insights',{'company_id':'U_1595','event_id':self.event['event_id'],'mode':'sources',
                                            'parent_run_id':'f'*32})
        self.assertEqual(run['status'],'sources_ready')

    def answer(self):
        position=copy.deepcopy(self.draft()['bank_positions'][0]);position['kind']='source_fact'
        return {'status':'answered','title':'Автомобили не устанавливают спрос на одежду',
                'summary':'Применимость к одежде не установлена; нужны данные клиента.',
                'scope_note':self.draft()['scope_note'],'points':[position],
                'uncertainties':['Продажи клиента неизвестны.'],'additional_queries':[]}

    def test_answer_accepts_only_verified_dates_period_and_labeled_company_codes(self):
        answer=self.answer();answer['points'][0]['text']='Банк (публикация 07.09.2026, наблюдение — июль 2026 г., 7М26) объясняет замедление непродовольственной розницы, в частности автомобилями.'
        answer['points'].append({'kind':'next_step','text':'Проверить у клиента U_1595 (ОКВЭД 47.71) продажи одежды; новость от 16.10.2026 не устанавливает спрос.'})
        check=lambda:check_answer(answer,self.profile,self.event,self.sources,self.settings.research_database)
        self.assertEqual(check(),[])
        for bad in ['Выручка вырастет на 7 процентов.','ОКВЭД 47.72: проверить продажи.','Дата 08.09.2026.', 'Продажи за июль 2025 г.']:
            answer['points'][-1]['text']=bad
            self.assertIn('unsupported_quantified_company_effect',check())

    def test_answer_date_format_is_verified_and_wrong_date_stays_rejected(self):
        answer=self.answer();point=answer['points'][0]
        point['text']=point['text'].replace('2026-09-07','07.09.2026')
        point.pop('publication_date',None)
        self.assertEqual(check_answer(answer,self.profile,self.event,self.sources,self.settings.research_database),[])
        point['text']=point['text'].replace('07.09.2026','08.09.2026')
        self.assertIn('bank_publication_date_missing',check_answer(answer,self.profile,self.event,self.sources,self.settings.research_database))

    def test_negated_indicator_does_not_assert_clothing_demand(self):
        answer=self.answer()
        answer['points'][0]['text']='2026-09-07: в июле автомобили — частный пример непродовольственного потребления, а не измеритель спроса на одежду.'
        self.assertEqual(check_answer(answer,self.profile,self.event,self.sources,self.settings.research_database),[])
        answer['points'][0]['text']='2026-09-07: в июле автомобили и одежда показывают замедление спроса.'
        self.assertIn('bank_does_not_establish_clothing_demand',check_answer(answer,self.profile,self.event,self.sources,self.settings.research_database))

    def test_previously_rejected_answer_is_only_an_untrusted_candidate_for_new_review(self):
        question='Почему автомобили не устанавливают спрос на одежду?'
        candidate=Agent(self.settings,self.store,None).run(self.profile,self.event,self.as_of,question=question)
        candidate.update(status='validation_failed',trace=[{'step':'draft_result','diagnostics':{'call_id':'d'*32}}])
        self.store.cache_set('synthetic-answer',{'result':self.answer(),'diagnostics':{'call_id':'d'*32}})
        parent={'status':'complete','insight':self.draft(),'bank_evidence':self.sources}
        stages=[]
        class Fake:
            def generate(inner,messages,identity,stage,max_tokens=None):
                stages.append(stage);self.assertEqual(stage,'review')
                return {'result':{'approved':True,'issues':[],'additional_queries':[]},'cached':False}
        run=Agent(self.settings,self.store,Fake()).run(self.profile,self.event,self.as_of,live=True,question=question,parent=parent,candidate_run=candidate)
        self.assertEqual(run['status'],'answered');self.assertEqual(stages,['review'])
        self.assertIn('candidate_recheck',[x['step'] for x in run['trace']])

    def test_feed_advertises_only_a_current_matching_main_result(self):
        app=Application(self.settings,seed=False);app.store.put_event(self.event)
        run=Agent(self.settings,app.store,None).run(self.profile,self.event,self.as_of)
        run.update(status='complete',insight=self.draft());app.store.save_run(run)
        query={'company_id':['U_1595'],'as_of':[str(self.as_of)]}
        row=next(x for x in app.dispatch_get('/api/news',query)['items'] if x['event']['event_id']==self.event['event_id'])
        self.assertEqual(row['cached_run_id'],run['run_id'])
        run['context_fingerprint']='old-approval';app.store.save_run(run)
        row=next(x for x in app.dispatch_get('/api/news',query)['items'] if x['event']['event_id']==self.event['event_id'])
        self.assertIsNone(row['cached_run_id'])

    def test_running_process_keeps_the_fingerprint_of_its_loaded_logic(self):
        expected=context_fingerprint(self.settings,self.profile,self.event,self.as_of,'cfo')[0]
        with patch('alfa_app.agent.Path.with_name',side_effect=AssertionError('Do not claim unloaded file edits')):
            self.assertEqual(context_fingerprint(self.settings,self.profile,self.event,self.as_of,'cfo')[0],expected)

    def test_current_answer_is_restored_without_api_but_old_approval_cannot_bypass_checks(self):
        app=Application(self.settings,seed=False);app.store.put_event(self.event)
        question='Почему автомобили не устанавливают спрос на одежду?'
        run=Agent(self.settings,app.store,None).run(self.profile,self.event,self.as_of,question=question)
        run.update(status='answered',answer=self.answer(),answer_origin='llm');app.store.save_run(run)
        body={'company_id':'U_1595','event_id':self.event['event_id'],'role':'cfo','question':question,
              'mode':'live','as_of':str(self.as_of)}
        with patch.object(app.agent,'run',side_effect=AssertionError('Current cached answer needs no API')):
            cached=app.dispatch_post('/api/insights',body)
            self.assertTrue(cached['restored_from_cache']);self.assertEqual(cached['run_id'],run['run_id'])
            self.assertEqual(cached['trace'][-1]['step'],'restore_result')
        run['context_fingerprint']='old-logic';app.store.save_run(run)
        # A queued agent response must have its ordinary persisted run identity.
        fresh=Agent(self.settings,app.store,None).run(self.profile,self.event,self.as_of,question=question)
        fresh.update(status='new_check_required');app.store.save_run(fresh)
        with patch.object(app.agent,'run',return_value=fresh) as agent:
            self.assertEqual(app.dispatch_post('/api/insights',body)['status'],'new_check_required')
            agent.assert_called_once()

    def test_stop_reason_uses_final_local_findings_not_previous_round_review(self):
        drafts=[]
        class Fake:
            def generate(inner,messages,identity,stage,max_tokens=None):
                if stage=='review':return {'result':{'approved':False,'issues':['Замечание только первого раунда.'],'additional_queries':[]},'cached':False}
                draft=valid_draft(json.loads(messages[-1]['content']));drafts.append(draft)
                if len(drafts)>1:draft['title']='Одежда: охлаждение спроса'
                return {'result':draft,'cached':False}
        run=Agent(self.settings,self.store,Fake()).run(self.profile,self.event,self.as_of,live=True)
        self.assertEqual(run['stop_detail']['issues'],['title_overstates_clothing_evidence'])

    def test_observation_period_cannot_be_replaced_by_publication_month(self):
        draft=self.draft();draft['bank_positions'][0]['text']=draft['bank_positions'][0]['text'].replace('в июле','в сентябре')
        self.assertIn('bank_observation_period_missing_or_changed',self.check(draft))

    def test_bank_does_not_establish_clothing_specific_slowdown(self):
        draft=self.draft();draft['bank_positions'][0]['text']='2026-09-07: банк установил в июле замедление спроса на одежду и автомобили.'
        self.assertIn('bank_does_not_establish_clothing_demand',self.check(draft))

    def test_full_page_has_supplying_context_and_exact_untruncated_quote(self):
        sent=model_evidence(self.sources,literal_quotes)
        self.assertEqual(sent[0]['text'],self.sources[0]['page_context'])
        self.assertIn('автомобил',sent[0]['text'])
        self.assertIn('ограничения со стороны предложения',sent[0]['text'])
        self.assertTrue(all(quote in self.sources[0]['text'] for quote in sent[0]['quote_options']))
        self.assertIn('в частности',self.draft()['bank_positions'][0]['quote'])

    def test_missing_data_question_returns_concrete_gaps_without_any_llm_or_search(self):
        class NoLLM:
            def generate(self,*args,**kwargs):raise AssertionError('No paid call needed to list missing data')
        with patch('alfa_app.agent.retrieve',side_effect=AssertionError('Private contracts must not be searched')):
            run=Agent(self.settings,self.store,NoLLM()).run(self.profile,self.event,self.as_of,live=True,
                question='Каких данных не хватает для полного вывода?')
        self.assertEqual(run['status'],'answered');self.assertEqual(run['task'],'missing_data')
        self.assertFalse(run['llm_request_attempted']);self.assertIsNone(run['insight'])
        text=json.dumps(run['answer'],ensure_ascii=False)
        for phrase in ['одежды','по месяцам','оборачиваемость','есть ли действующий долг','единицы']:
            self.assertIn(phrase,text)

    def test_data_gaps_change_for_construction_and_role(self):
        other=json.loads((ROOT/'data/company_U_83932.json').read_text(encoding='utf-8'))
        agent=Agent(self.settings,self.store,None)
        retail=agent.run(self.profile,self.event,self.as_of,question='Каких данных не хватает?',role='cfo')['answer']
        build=agent.run(other,self.event,self.as_of,question='Каких данных не хватает?',role='ceo')['answer']
        self.assertNotEqual(retail,build);self.assertIn('строительного проекта',json.dumps(build,ensure_ascii=False))

    def test_task_routing_and_question_not_dropped_from_search_plan(self):
        self.assertEqual(task_for(''),'insight')
        self.assertEqual(task_for('Поясни, почему это связано с компанией'),'explain')
        self.assertEqual(task_for('Найди дополнительные источники о спросе'),'sources')
        q='Найди источники о продажах одежды'
        self.assertIn(q,plan_queries(self.profile,self.event,q))

    def test_source_request_does_not_generate_even_when_live_mode_requested(self):
        class NoLLM:
            def generate(self,*args,**kwargs):raise AssertionError('Sources task does not generate')
        run=Agent(self.settings,self.store,NoLLM()).run(self.profile,self.event,self.as_of,live=True,
               question='Найди дополнительные источники о спросе')
        self.assertEqual(run['status'],'sources_ready');self.assertEqual(run['task'],'sources')

    def test_explanation_has_own_shape_and_does_not_require_six_insight_sections(self):
        sent=[]
        class Fake:
            def generate(inner,messages,identity,stage,max_tokens=None):
                context=json.loads(messages[-1]['content']);sent.append((stage,context))
                answer={'status':'answered','title':'Границы вывода','summary':'Влияние на компанию не установлено.',
                        'points':[{'kind':'limitation','text':'Нужна история продаж одежды клиента.','evidence_ids':[]}],
                        'scope_note':'Наблюдение об автомобилях не устанавливает динамику одежды.','uncertainties':['Продажи не установлены.']}
                return {'result':{'approved':True,'issues':[],'additional_queries':[]} if stage=='review' else answer,'cached':False}
        run=Agent(self.settings,self.store,Fake()).run(self.profile,self.event,self.as_of,live=True,question='Поясни готовый вывод')
        self.assertEqual(run['status'],'answered');self.assertIsNone(run['insight'])
        self.assertEqual([s[0] for s in sent],['draft','review'])
        self.assertIn('points',sent[0][1]['required_shape']);self.assertNotIn('risks',sent[0][1]['required_shape'])

    def test_response_rejection_is_distinct_from_missing_sources(self):
        class Bad:
            def generate(inner,messages,identity,stage,max_tokens=None):
                draft=self.draft();draft['actions'][0]['evidence_ids']=['fake-id']
                return {'result':draft,'cached':False}
        run=Agent(self.settings,self.store,Bad()).run(self.profile,self.event,self.as_of,live=True)
        self.assertEqual(run['status'],'validation_failed');self.assertIsNone(run['insight'])
        self.assertIn('unknown_or_missing_evidence_id',run['check_issues'])
        self.assertTrue(run['stop_detail']['next_step'])

    def test_fingerprint_covers_company_role_question_news_date_model_bank_and_logic(self):
        def key(settings=self.settings,profile=self.profile,event=self.event,as_of=self.as_of,role='cfo',question=''):
            return context_fingerprint(settings,profile,event,as_of,role,question)[0]
        base=key();other=copy.deepcopy(self.profile);other['company']['products']['credit']=False
        changed=dict(self.event,content_sha256='changed-news')
        variants=[key(profile=other),key(role='ceo'),key(question='Почему?'),key(event=changed),key(event=dict(self.event,title='Изменённый заголовок')),key(as_of=date(2026,10,20)),
                  key(settings=replace(self.settings,model='z-ai/glm-5.3-flash'))]
        index=Path(self.temp.name)/'changed-research.sqlite';index.write_bytes(self.settings.research_database.read_bytes()+b'\n')
        variants.append(key(settings=replace(self.settings,research_database=index)))
        with patch('alfa_app.agent.PROMPT_VERSION','synthetic-future-version'):variants.append(key())
        self.assertTrue(all(value!=base for value in variants))

    def test_historical_approval_is_not_served_as_current_and_database_is_unchanged(self):
        app=Application(self.settings,seed=False);app.store.put_event(self.event)
        old={'run_id':'a'*32,'company_id':self.profile['company_id'],'role':'cfo','question':'','analysis_as_of':str(self.as_of),
             'event':self.event,'status':'complete','insight':self.draft(),'context_fingerprint':'outdated'}
        app.store.save_run(old)
        current=app.dispatch_get('/api/runs/'+old['run_id'],{})
        self.assertEqual(current['status'],'stale_result');self.assertIsNone(current['insight'])
        self.assertEqual(app.store.run(old['run_id'])['status'],'complete')

    def test_cached_approved_draft_still_runs_new_local_scope_check(self):
        class StaleCache:
            def generate(inner,messages,identity,stage,max_tokens=None):
                self.assertEqual(stage,'draft')
                draft=self.draft();draft['title']='Одежда: охлаждение спроса'
                return {'result':draft,'cached':True}
        run=Agent(self.settings,self.store,StaleCache()).run(self.profile,self.event,self.as_of,live=True)
        self.assertEqual(run['status'],'validation_failed');self.assertIsNone(run['insight'])
        self.assertNotIn('model_review',[s['step'] for s in run['trace']])

    def test_private_contract_question_is_filtered_before_initial_search(self):
        searched=[]
        original=retrieve
        def observed(database,query,*args,**kwargs):
            searched.append(query);return original(database,query,*args,**kwargs)
        with patch('alfa_app.agent.retrieve',observed):
            run=Agent(self.settings,self.store,None).run(self.profile,self.event,self.as_of,
                question='Найди условия кредитов клиента U_1595',task='sources',live=True)
        self.assertTrue(searched);self.assertTrue(all('U_1595' not in query for query in searched))
        self.assertIn('отсутствуют',run['stop_detail']['message'])

    def test_genuinely_empty_retrieval_is_reported_as_insufficient_evidence(self):
        with patch('alfa_app.agent.retrieve',return_value=[]):
            run=Agent(self.settings,self.store,None).run(self.profile,self.event,self.as_of,live=True)
        self.assertEqual(run['status'],'insufficient_evidence');self.assertFalse(run['llm_request_attempted'])
        self.assertTrue(run['stop_detail']['next_step'])

    def test_malformed_claim_group_reports_schema_error_instead_of_crashing(self):
        draft=self.draft();draft['risks']=None
        self.assertIn('invalid_or_missing_risks',self.check(draft))

    def test_explanation_uses_dedicated_prompt_and_only_referenced_parent_pages(self):
        contexts=[]
        class Fake:
            def generate(inner,messages,identity,stage,max_tokens=None):
                context=json.loads(messages[-1]['content'])
                if stage=='review':return {'result':{'approved':True,'issues':[],'additional_queries':[]},'cached':False}
                if context['task']=='insight':return {'result':valid_draft(context),'cached':False}
                contexts.append((messages[0]['content'],context))
                return {'result':{'status':'answered','title':'Границы','summary':'Влияние не установлено.',
                    'points':[{'kind':'limitation','text':'Нужны продажи клиента.','evidence_ids':[]}],
                    'uncertainties':['Данных продаж нет.']},'cached':False}
        agent=Agent(self.settings,self.store,Fake())
        main=agent.run(self.profile,self.event,self.as_of,live=True)
        answer=agent.run(self.profile,self.event,self.as_of,live=True,question='Поясни вывод',parent=main)
        self.assertEqual(answer['status'],'answered')
        self.assertNotIn('В news_facts',contexts[0][0])
        self.assertNotIn('risks',contexts[0][1]['required_shape'])
        self.assertNotIn('actions',contexts[0][1]['parent_insight'])
        self.assertIn('reuse_sources',[step['step'] for step in answer['trace']])
        self.assertNotIn('retrieve',[step['step'] for step in answer['trace']])


if __name__=='__main__':unittest.main()

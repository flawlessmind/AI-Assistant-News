"""Synthetic gateway responses only. No paid API calls or real credentials."""
from datetime import date
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest

from alfa_app.agent import Agent,check_draft,literal_quotes,research_queries,select_evidence
from alfa_app.config import ROOT, Settings
from alfa_app.llm import LLMClient, LLMError, decode_completion, parse_json_reply
from alfa_app.store import Store


def envelope(content='{"ok":true}', finish_reason='stop', **message_fields):
    return json.dumps({'choices':[{'finish_reason':finish_reason,
                                  'message':dict(content=content,**message_fields)}],
                       'usage':{'prompt_tokens':42,'completion_tokens':1800,'total_tokens':1842,
                                'completion_tokens_details':{'reasoning_tokens':1700}}}).encode()


class ResponseDecodingTests(unittest.TestCase):
    def assert_failure(self, raw, reason):
        with self.assertRaises(LLMError) as caught:
            decode_completion(raw,1800)
        self.assertEqual(caught.exception.code,'invalid_response')
        self.assertEqual(caught.exception.diagnostics['reason'],reason)
        return caught.exception

    def test_length_failure_keeps_usage_even_if_content_is_parseable(self):
        error=self.assert_failure(envelope(finish_reason='length'),'output_truncated')
        self.assertEqual(error.diagnostics['finish_reason'],'length')
        self.assertEqual(error.diagnostics['requested_max_tokens'],1800)
        self.assertEqual(error.diagnostics['usage']['completion_tokens'],1800)
        self.assertEqual(error.diagnostics['usage']['completion_tokens_details']['reasoning_tokens'],1700)

    def test_truncated_text_parts_are_measured_but_not_accepted(self):
        error=self.assert_failure(envelope([{'type':'text','text':'{"ok":true}'}],finish_reason='length'),'output_truncated')
        self.assertEqual(error.diagnostics['content_chars'],11)

    def test_reasoning_is_diagnosed_but_never_used_as_final_answer(self):
        private='SYNTHETIC_PRIVATE_REASONING {"ok":true}'
        error=self.assert_failure(envelope(None,reasoning_content=private),'reasoning_without_answer')
        self.assertEqual(error.diagnostics['reasoning_chars'],len(private))
        self.assertNotIn(private,json.dumps(error.public()))

    def test_empty_answer_is_distinct_from_bad_json(self):
        self.assert_failure(envelope('   '),'empty_content')
        error=self.assert_failure(envelope('{"ok":'),'invalid_content_json')
        self.assertIn('json_error_line',error.diagnostics)
        self.assertIn('json_error_column',error.diagnostics)

    def test_incorrect_envelopes_are_bounded_public_errors(self):
        for value in [[],None,42]:
            with self.subTest(value=value):
                self.assert_failure(json.dumps(value).encode(),'unexpected_envelope')
        for value in [{},{'choices':None},{'choices':{}},{'choices':[]},{'choices':[None]}]:
            with self.subTest(value=value):
                self.assert_failure(json.dumps(value).encode(),'missing_choices')
        self.assert_failure(b'not JSON','invalid_envelope_json')
        self.assert_failure(b'{"choices":[{"message":null}]}','missing_message')

    def test_unknown_provider_strings_and_error_body_are_not_echoed(self):
        secret='synthetic-do-not-echo-credential'
        error=self.assert_failure(envelope(secret,finish_reason=secret),'generation_not_completed')
        self.assertNotIn(secret,json.dumps(error.public()))
        error=self.assert_failure(json.dumps({'error':{'message':secret}}).encode(),'gateway_error_envelope')
        self.assertNotIn(secret,json.dumps(error.public()))

    def test_refusal_or_tool_result_cannot_be_published_as_insight(self):
        self.assert_failure(envelope(finish_reason='content_filter'),'response_refused')
        self.assert_failure(envelope(refusal='synthetic refusal'),'response_refused')
        self.assert_failure(envelope(tool_calls=[{'id':'synthetic'}]),'generation_not_completed')

    def test_text_parts_are_joined_without_dropping_other_parts(self):
        result,usage,_=decode_completion(envelope([{'type':'text','text':'{"ok":'},
                                                   {'type':'text','text':'true}'}]),1800)
        self.assertEqual(result,{'ok':True})
        self.assertEqual(usage['prompt_tokens'],42)
        self.assert_failure(envelope([{'type':'text','text':'{"ok":true}'},
                                      {'type':'refusal','refusal':'synthetic'}]),'unsupported_content_parts')
        self.assert_failure(envelope({'ok':True}),'unsupported_content_type')

    def test_complete_fenced_json_is_accepted_but_truncated_json_is_not_repaired(self):
        result,_,_=decode_completion(envelope('```json\n{"ok":true}\n```'),1800)
        self.assertEqual(result,{'ok':True})
        self.assert_failure(envelope('{"ok":true'),'invalid_content_json')
        self.assert_failure(envelope('[{"ok":true}]'),'invalid_content_json')
        for text in ['{"approved":false,"approved":true}','{"value":NaN}']:
            with self.subTest(text=text),self.assertRaises(ValueError):
                parse_json_reply(text)

    def test_invalid_usage_values_do_not_crash_or_leak_unknown_fields(self):
        response=json.loads(envelope())
        response['usage']={'prompt_tokens':True,'completion_tokens':-1,'secret':'synthetic',
                           'total_tokens':12,'completion_tokens_details':{'reasoning_tokens':'unknown'}}
        _,usage,_=decode_completion(json.dumps(response).encode(),1800)
        self.assertEqual(usage,{'total_tokens':12})


class FailurePersistenceTests(unittest.TestCase):
    def setUp(self):
        base=ROOT/'.cache/tests'
        base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base)
        self.settings=Settings(api_key='synthetic-only',allow_live=True,max_output_tokens=1800,runtime_dir=Path(self.temp.name))
        self.store=Store(self.settings.runtime_dir/'service.sqlite')

    def tearDown(self):
        self.temp.cleanup()

    def test_invalid_response_usage_is_persisted_without_extra_transport_retry(self):
        calls=[]
        def send(*args):
            calls.append(1)
            return 200,envelope(None,'length',reasoning_content='synthetic private text')
        client=LLMClient(self.settings,self.store,send)
        with self.assertRaises(LLMError):
            client.generate([], 'synthetic-format-failure')
        self.assertEqual(len(calls),1)
        with self.store.connect() as db:
            row=db.execute('SELECT status,usage FROM calls').fetchone()
        self.assertEqual(row[0],'output_truncated')
        self.assertEqual(json.loads(row[1])['completion_tokens'],1800)

    def test_agent_stops_after_one_length_recovery_and_keeps_saved_metadata(self):
        calls=[]
        def send(*args):
            calls.append(1)
            return 200,envelope(None,'length',reasoning_content='synthetic private text')
        client=LLMClient(self.settings,self.store,send)
        profile=json.loads((ROOT/'data/company_U_1595.json').read_text(encoding='utf-8'))
        events=[json.loads(line) for line in (ROOT/'data/news_snapshot.jsonl').read_text(encoding='utf-8').splitlines()]
        event=next(event for event in events if 'инфляц' in event['text'].lower() and 'сентябре цены' in event['title'])
        run=Agent(self.settings,self.store,client).run(profile,event,date(2026,10,21),live=True)
        self.assertEqual(run['status'],'generation_unavailable')
        self.assertTrue(run['llm_request_attempted'])
        self.assertIsNone(run['insight'])
        self.assertEqual(len(calls),2)
        self.assertNotIn('replan',[step['step'] for step in run['trace']])
        self.assertEqual(sum(step['step']=='generation_recovery' for step in run['trace']),1)
        error_step=next(step for step in run['trace'] if step['step']=='response_error')
        self.assertEqual(error_step['diagnostics']['reason'],'output_truncated')
        self.assertEqual(self.store.run(run['run_id'])['error']['diagnostics']['requested_max_tokens'],1800)
        self.assertNotIn('synthetic private text',json.dumps(run))

    def test_review_limit_is_independent_of_short_draft_limit_and_persisted(self):
        self.settings.max_output_tokens=128
        self.settings.review_max_tokens=2048
        sent=[]
        def send(method,url,headers,body,timeout):
            sent.append(json.loads(body))
            return 200,envelope()
        client=LLMClient(self.settings,self.store,send)
        for stage in ['draft','review']:
            response=client.generate([], 'independent-limits', stage)
            self.assertEqual(response['diagnostics']['stage'],stage)
        self.assertEqual([body['max_tokens'] for body in sent],[128,2048])
        with self.assertRaises(ValueError):
            client.generate([], 'invalid-limit', 'draft',129)
        with self.store.connect() as db:
            rows=db.execute('SELECT diagnostics FROM calls ORDER BY rowid').fetchall()
        self.assertEqual([json.loads(row[0])['requested_max_tokens'] for row in rows],[128,2048])
        self.assertNotIn('response_format',sent[0])
        self.assertNotIn('thinking',sent[0])
        self.assertNotIn('reasoning',sent[0])

    def test_context_size_failure_does_not_spend_a_request(self):
        client=LLMClient(self.settings,self.store,lambda *args:self.fail('transport must not run'))
        with self.assertRaises(LLMError) as caught:
            client.generate([{'role':'user','content':'x'*80001}], 'oversized')
        self.assertEqual(caught.exception.code,'context_too_large')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],0)

    def agent_fixture(self,send):
        from test_service import synthetic_event
        profile=json.loads((ROOT/'data/company_U_1595.json').read_text(encoding='utf-8'))
        return Agent(self.settings,self.store,LLMClient(self.settings,self.store,send)),profile,synthetic_event()

    def test_review_truncation_has_own_stage_and_never_publishes_draft(self):
        from test_service import completion,valid_draft
        calls=[]
        def send(method,url,headers,body,timeout):
            request=json.loads(body); calls.append(request)
            context=json.loads(request['messages'][-1]['content'])
            if len(calls)==1:
                self.assertLessEqual(len(context['news']['text']),3500)
                self.assertTrue(all(x['page_context_sha256'] and len(x['text'])<=7000 for x in context['bank_evidence']))
                self.assertLess(len(body),80000)
                return completion(valid_draft(context))
            self.assertNotIn('required_shape',context['sources'])
            self.assertNotEqual(request['messages'][0]['content'],calls[0]['messages'][0]['content'])
            return 200,envelope(None,'length',reasoning_content='synthetic private reasoning')
        agent,profile,event=self.agent_fixture(send)
        run=agent.run(profile,event,date(2026,10,21),live=True)
        self.assertIsNone(run['insight'])
        self.assertEqual(run['error']['diagnostics']['stage'],'review')
        self.assertEqual(run['error']['diagnostics']['requested_max_tokens'],6144)
        self.assertEqual(len(calls),2)
        self.assertNotIn('replan',[step['step'] for step in run['trace']])
        self.assertNotIn('synthetic private reasoning',json.dumps(run))

    def test_invalid_review_schema_stops_without_unrelated_retrieval(self):
        from test_service import completion,valid_draft
        calls=[]
        def send(method,url,headers,body,timeout):
            request=json.loads(body); calls.append(request)
            return completion(valid_draft(json.loads(request['messages'][-1]['content']))) if len(calls)==1 else completion({'approved':'yes','issues':[]})
        agent,profile,event=self.agent_fixture(send)
        run=agent.run(profile,event,date(2026,10,21),live=True)
        self.assertEqual(run['error']['code'],'invalid_review_schema')
        self.assertIsNone(run['insight'])
        self.assertEqual(len(calls),2)
        self.assertNotIn('replan',[step['step'] for step in run['trace']])

    def test_missing_required_section_is_not_a_complete_insight(self):
        from test_service import completion,valid_draft
        calls=[]
        def send(method,url,headers,body,timeout):
            calls.append(1)
            draft=valid_draft(json.loads(json.loads(body)['messages'][-1]['content']))
            draft['risks']=[]
            return completion(draft)
        agent,profile,event=self.agent_fixture(send)
        run=agent.run(profile,event,date(2026,10,21),live=True)
        self.assertEqual(run['error']['code'],'invalid_draft_schema')
        self.assertIsNone(run['insight'])
        self.assertEqual(len(calls),1)

    def test_call_diagnostics_migration_preserves_existing_rows(self):
        path=self.settings.runtime_dir/'legacy.sqlite'
        db=sqlite3.connect(path)
        db.execute('CREATE TABLE calls(id TEXT PRIMARY KEY,scope TEXT,period TEXT,status TEXT,model TEXT,usage TEXT,created TEXT)')
        db.execute("INSERT INTO calls VALUES('old','synthetic','2026-10-21','ok','test','{}','old-date')")
        db.commit(); db.close()
        store=Store(path)
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT id,diagnostics FROM calls').fetchone(),('old','{}'))

    def test_literal_quote_options_are_unchanged_source_substrings(self):
        text='Цены в регионах росли сдержанно. Мы полагаем, что динамика спроса может измениться. Неисправленный OCR остоется в тексте источника.'
        options=literal_quotes(text)
        self.assertTrue(options)
        self.assertTrue(all(24<=len(quote)<=120 and quote in text for quote in options))
        self.assertIn('Неисправленный OCR остоется в тексте источника.',options)

    def test_verified_publication_dates_do_not_allow_changed_dates_or_ocr_numbers(self):
        from test_service import synthetic_event,valid_draft
        from alfa_research import retrieve
        profile=json.loads((ROOT/'data/company_U_1595.json').read_text(encoding='utf-8'))
        event=synthetic_event()
        sources=retrieve(self.settings.research_database,'инфляция',date(2026,10,21),limit=1)
        context={'bank_evidence':sources,'news':event}
        draft=valid_draft(context)
        published=date.fromisoformat(sources[0]['publication_date'])
        months=['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря']
        for value in [published.isoformat(),published.strftime('%d.%m.%Y'),f'{published.day} {months[published.month-1]} {published.year}']:
            draft['bank_positions'][0]['text']='Качественная позиция банка от '+value+'.'
            self.assertNotIn('unreviewed_bank_numeric_claim',check_draft(draft,profile,event,sources,self.settings.research_database))
        for value in ['2099-01-01','01.01.2025','ставка 14 процентов','2026']:
            draft['bank_positions'][0]['text']='Позиция банка: '+value+'.'
            self.assertIn('unreviewed_bank_numeric_claim',check_draft(draft,profile,event,sources,self.settings.research_database))

    def test_conditional_flag_does_not_justify_credit_price_increase_or_fake_forecast(self):
        from test_service import synthetic_event,valid_draft
        from alfa_research import retrieve
        profile=json.loads((ROOT/'data/company_U_1595.json').read_text(encoding='utf-8'))
        event=synthetic_event()
        sources=retrieve(self.settings.research_database,'инфляция',date(2026,10,21),limit=1)
        draft=valid_draft({'bank_evidence':sources,'news':event})
        draft['risks'][0]['text']='При сохранении жесткой политики обслуживание кредитных обязательств может подорожать.'
        self.assertIn('credit_cost_requires_contract_condition',check_draft(draft,profile,event,sources,self.settings.research_database))
        draft['bank_positions'][0]['text']='Прогноз банка о замедлении потребления.'
        draft['bank_positions'][0]['quote']=sources[0]['text'][:90]
        self.assertIn('forecast_not_supported_by_quote',check_draft(draft,profile,event,sources,self.settings.research_database))

    def test_review_findings_reach_next_draft_and_registration_is_not_exposure(self):
        from test_service import completion,valid_draft
        contexts=[];review_calls=[]
        def send(method,url,headers,body,timeout):
            request=json.loads(body)
            context=json.loads(request['messages'][-1]['content'])
            if 'draft' in context:
                review_calls.append(1)
                return completion({'approved':len(review_calls)>1,
                    'issues':[] if len(review_calls)>1 else ['Конкретное синтетическое замечание об условии влияния.'],
                    'additional_queries':[] if len(review_calls)>1 else ['потребление спрос']})
            contexts.append(context)
            self.assertNotIn('region',context['profile']['company'])
            self.assertNotIn('revenue',context['profile']['company'])
            self.assertNotIn('fts_activity_flag',context['profile']['company'])
            return completion(valid_draft(context))
        agent,profile,event=self.agent_fixture(send)
        run=agent.run(profile,event,date(2026,10,21),live=True)
        self.assertEqual(run['status'],'complete')
        self.assertEqual(contexts[0]['prior_review_issues'],[])
        self.assertEqual(contexts[1]['prior_review_issues'],['Конкретное синтетическое замечание об условии влияния.'])

    def test_additional_retrieval_retains_four_anchors_and_admits_new_sources(self):
        sources=[{'evidence_id':f'synthetic_{i}','source_id':'synthetic','publication_date':'2026-09-01'} for i in range(10)]
        anchors=sources[:6]
        selected=select_evidence({item['evidence_id']:item for item in sources},
                                 {item['evidence_id'] for item in sources[6:]},None,anchors)
        self.assertEqual(selected[:4],anchors[:4])
        self.assertEqual(len(selected),6)
        self.assertTrue(all(item['evidence_id'] in {'synthetic_6','synthetic_7','synthetic_8','synthetic_9'} for item in selected[4:]))

    def test_private_company_queries_are_not_sent_to_macro_research(self):
        profile={'company':{'industry_okved_code':'47.71'}}
        queries,unavailable=research_queries(['Условия кредитов клиента U_1595','Сумма договора компании','инфляция непродовольственные товары'],profile)
        self.assertEqual(queries,['инфляция непродовольственные товары'])
        self.assertEqual(len(unavailable),2)
        fallback,_=research_queries(['Договоры U_1595'],profile)
        self.assertTrue(fallback)
        self.assertTrue(all('U_1595' not in query for query in fallback))


if __name__=='__main__':
    unittest.main()

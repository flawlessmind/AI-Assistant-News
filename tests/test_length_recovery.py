"""Bounded recovery uses only synthetic transports; no paid or external calls."""
from dataclasses import replace
from datetime import date
import copy
import json
from pathlib import Path
import tempfile
import unittest
from alfa_app.agent import Agent,context_fingerprint,length_recovery_context,SYSTEM,RECOVERY_SYSTEM
from alfa_app.config import MODELS,ROOT,Settings
from alfa_app.llm import LLMClient,LLMError
from alfa_app.store import Store
from test_service import completion,valid_draft
from test_llm_responses import envelope


class LengthRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(dir=ROOT/'.cache/tests')
        self.addCleanup(self.temporary.cleanup)
        self.settings=Settings(api_key='synthetic',allow_live=True,max_rounds=1,runtime_dir=Path(self.temporary.name),review_model=MODELS[0])
        self.store=Store(self.settings.runtime_dir/'service.sqlite')
        self.profile=json.loads((ROOT/'data/company_U_1595.json').read_text('utf-8'))
        self.event=next(json.loads(line) for line in (ROOT/'data/news_snapshot.jsonl').read_text('utf-8').splitlines()
                        if json.loads(line)['event_id']=='news_9f10b60aed544eaa32d4')

    def run_with(self,send,settings=None):
        settings=settings or self.settings
        return Agent(settings,self.store,LLMClient(settings,self.store,send)).run(self.profile,self.event,date(2026,10,23),live=True)

    def test_length_uses_an_allowlisted_recovery_then_normal_validation_and_review(self):
        requests=[]
        def send(method,url,headers,body,timeout):
            request=json.loads(body);requests.append(request)
            if len(requests)==1:return 200,envelope('{"unfinished":','length',reasoning_content='PRIVATE_SYNTHETIC_REASONING')
            context=json.loads(request['messages'][-1]['content'])
            if len(requests)==2:
                self.assertNotIn('provenance',context)
                self.assertLessEqual(len(context['bank_evidence']),2)
                return completion(valid_draft(context))
            return completion({'approved':True,'issues':[],'additional_queries':[]})
        run=self.run_with(send)
        self.assertEqual(run['status'],'complete')
        self.assertTrue(run['generation_recovered'])
        self.assertEqual([r['model'] for r in requests],[MODELS[3],MODELS[0],MODELS[0]])
        self.assertEqual(requests[0]['messages'][0]['content'],SYSTEM)
        self.assertEqual(requests[1]['messages'][0]['content'],RECOVERY_SYSTEM)
        self.assertEqual(run['model'],MODELS[0])
        self.assertNotIn('PRIVATE_SYNTHETIC_REASONING',json.dumps(run))
        with self.store.connect() as db:
            calls=db.execute('SELECT status,model FROM calls ORDER BY created').fetchall()
        self.assertEqual([row[0] for row in calls],['output_truncated','ok','ok'])

    def test_reasoning_without_final_answer_also_recovers(self):
        count=0
        def send(method,url,headers,body,timeout):
            nonlocal count
            count+=1
            if count==1:return 200,envelope(None,reasoning_content='PRIVATE_SYNTHETIC_REASONING')
            context=json.loads(json.loads(body)['messages'][-1]['content'])
            return completion(valid_draft(context) if count==2 else {'approved':True,'issues':[],'additional_queries':[]})
        self.assertEqual(self.run_with(send)['status'],'complete')
        self.assertEqual(count,3)

    def test_recovery_does_not_drop_page_text_quotes_or_scope_constraints(self):
        sources=[{'evidence_id':str(i),'text':'Full page including the qualifier '+str(i),
                  'quote_options':['Exact unchanged quote '+str(i)],'usage_constraints':['Keep the original segment and period.']} for i in range(4)]
        context={'bank_evidence':sources,'provenance':{'hash':'synthetic'},'news':{'text':'Full news'},'required_shape':{'scope_note':'required'}}
        before=copy.deepcopy(context);compact=length_recovery_context(context)
        self.assertEqual(context,before)
        self.assertEqual(compact['bank_evidence'],sources[:2])
        self.assertEqual(compact['news'],context['news'])
        self.assertEqual(compact['required_shape'],context['required_shape'])

    def test_second_length_failure_stops_without_another_model_or_round(self):
        count=0
        def send(*args):
            nonlocal count
            count+=1;return 200,envelope('{}','length')
        run=self.run_with(send,replace(self.settings,max_rounds=3))
        self.assertEqual(count,2)
        self.assertEqual(run['status'],'generation_unavailable')
        self.assertIsNone(run['insight'])
        self.assertIn('после повторной попытки',run['stop_detail']['message'])

    def test_authentication_budget_and_invalid_json_do_not_trigger_recovery(self):
        for index,response in enumerate([(401,b'{}'),(429,b'{"error":"Budget has been exceeded"}'),(200,envelope('{broken'))]):
            with self.subTest(response=response[0]):
                calls=[]
                def send(*args):calls.append(1);return response
                run=self.run_with(send,replace(self.settings,api_key='synthetic-'+str(index)))
                self.assertEqual(len(calls),1)
                self.assertNotIn('generation_recovery',[step['step'] for step in run['trace']])

    def test_recovery_cannot_bypass_the_same_shared_call_cap(self):
        calls=[]
        def send(*args):calls.append(1);return 200,envelope('{}','length')
        run=self.run_with(send,replace(self.settings,max_calls_per_period=1))
        self.assertEqual(len(calls),1)
        self.assertEqual(run['error']['code'],'local_call_limit')

    def test_recovery_model_is_allowlisted_and_part_of_the_context_fingerprint(self):
        with self.assertRaises(ValueError):replace(self.settings,recovery_model='unapproved')
        one=context_fingerprint(self.settings,self.profile,self.event,date(2026,10,23),'cfo')[0]
        two=context_fingerprint(replace(self.settings,recovery_model=MODELS[1]),self.profile,self.event,date(2026,10,23),'cfo')[0]
        self.assertNotEqual(one,two)

    def test_recovery_still_rejects_invalid_quotes(self):
        calls=[]
        def send(method,url,headers,body,timeout):
            calls.append(1)
            if len(calls)==1:return 200,envelope('{}','length')
            context=json.loads(json.loads(body)['messages'][-1]['content'])
            draft=valid_draft(context);draft['bank_positions'][0]['quote']='This quote does not exist on the bank page.'
            return completion(draft)
        run=self.run_with(send)
        self.assertEqual(run['status'],'validation_failed')
        self.assertIn('bank_quote_not_found',run['check_issues'])
        self.assertEqual(len(calls),2)

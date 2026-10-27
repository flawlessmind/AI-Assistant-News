"""Regressions from actual rejected output; synthetic transports never spend API budget."""
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from alfa_app.agent import Agent, check_draft
from alfa_app.config import ROOT, Settings
from alfa_app.evidence import attach_page_context, semantic_issues
from alfa_app.store import Store
from alfa_research import retrieve
from test_service import valid_draft


class LiveRegressionTests(unittest.TestCase):
    def setUp(self):
        self.profile=json.loads((ROOT/'data/company_U_1595.json').read_text(encoding='utf-8'))
        self.event=next(json.loads(line) for line in (ROOT/'data/news_snapshot.jsonl').read_text(encoding='utf-8').splitlines()
                        if json.loads(line)['event_id']=='news_9f10b60aed544eaa32d4')

    def test_currency_weakness_title_does_not_assert_weaker_clothing_demand(self):
        title='Ослабление рубля и цены непродовольственных товаров: что это значит для розничной торговли одеждой?'
        self.assertNotIn('title_overstates_clothing_evidence',semantic_issues({'title':title},self.profile,[]))
        for title in ['Одежда: ослабление спроса','Одежда: снижение продаж','Торговля одеждой: охлаждение спроса']:
            self.assertIn('title_overstates_clothing_evidence',semantic_issues({'title':title},self.profile,[]))

    def test_continuation_forecast_is_supported_by_real_unchanged_quote(self):
        evidence=attach_page_context(ROOT/'data/research.sqlite',retrieve(ROOT/'data/research.sqlite',
            'ослабление рубля непродовольственный сегмент',date(2026,10,22),source_id='alfa_inflation',limit=1))
        draft=valid_draft({'bank_evidence':evidence,'news':self.event})
        quote='ой стороны, ослабление курсо рубля, на\nнаш взгляд, продолжит воздейстзозоть на\nдинамику цен в непродовольственном сегменте.'
        self.assertIn(quote,evidence[0]['text'])
        draft['bank_positions'][0].update(text='Прогноз банка: ослабление курса рубля продолжит влиять на цены непродовольственных товаров.',
                     publication_date=evidence[0]['publication_date'],quote=quote)
        self.assertNotIn('forecast_not_supported_by_quote',check_draft(draft,self.profile,self.event,evidence,ROOT/'data/research.sqlite'))
        draft['bank_positions'][0]['quote']='Недельная инфляция по сегментам'
        self.assertIn('forecast_not_supported_by_quote',check_draft(draft,self.profile,self.event,evidence,ROOT/'data/research.sqlite'))

    def test_failed_review_can_reuse_only_a_complete_cached_draft_then_review_all_cited_pages(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings=Settings(api_key='synthetic',allow_live=True,max_rounds=1,runtime_dir=Path(temporary))
            store=Store(settings.runtime_dir/'service.sqlite')
            source_run=Agent(settings,store,None).run(self.profile,self.event,date(2026,10,22))
            source_run['status']='generation_unavailable'
            source_run['trace'].append({'step':'draft_result','diagnostics':{'call_id':'test-complete-draft'}})
            # This fixture is a generic report without the automotive scope rule.
            evidence=source_run['bank_evidence']
            draft=valid_draft({'bank_evidence':evidence,'news':self.event})
            calls=[]
            class Reviewer:
                def generate(inner,messages,identity,stage,max_tokens=None):
                    calls.append(stage)
                    context=json.loads(messages[-1]['content'])
                    cited={eid for group in ['bank_positions','interpretations','risks','opportunities','actions']
                           for item in context['draft'].get(group,[]) for eid in item.get('evidence_ids',[])}
                    sent=context['sources']['bank_evidence']
                    self.assertEqual({row['evidence_id'] for row in sent},cited-{self.event['event_id']})
                    self.assertTrue(all(row['text'] and row.get('page_context_sha256') for row in sent))
                    return {'result':{'approved':True,'issues':[],'additional_queries':[]},'cached':False}
            from unittest.mock import patch
            with patch.object(store,'cached_response_for_call',return_value={'result':draft}):
                run=Agent(settings,store,Reviewer()).run(self.profile,self.event,date(2026,10,22),live=True,candidate_run=source_run)
            self.assertEqual(run['status'],'complete')
            self.assertEqual(calls,['review'])
            self.assertIn('candidate_recheck',[step['step'] for step in run['trace']])

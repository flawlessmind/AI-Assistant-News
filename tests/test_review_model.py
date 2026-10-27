"""Explicit stage models share one key ledger; no external transport in tests."""
from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from alfa_app.agent import context_fingerprint
from alfa_app.config import MODELS,ROOT,Settings
from alfa_app.llm import LLMClient,LLMError
from alfa_app.store import Store
from test_service import completion


class StageModelTests(unittest.TestCase):
    def test_review_model_allowlist_and_public_identity(self):
        with self.assertRaises(ValueError):Settings(review_model='unapproved-model')
        settings=Settings(model=MODELS[3],review_model=MODELS[0])
        self.assertEqual(settings.model_for('draft'),MODELS[3])
        self.assertEqual(settings.public()['review_model'],MODELS[0])

    def test_both_models_share_one_cap_and_keep_the_actual_model_in_receipts(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(api_key='synthetic',allow_live=True,review_model=MODELS[0],max_calls_per_period=2,runtime_dir=Path(folder))
            store=Store(Path(folder)/'service.sqlite');models=[]
            def send(method,url,headers,body,timeout):
                models.append(json.loads(body)['model']);return completion({'approved':True,'issues':[],'additional_queries':[]})
            client=LLMClient(settings,store,send)
            draft=client.generate([],'one','draft');review=client.generate([],'one','review')
            self.assertEqual(models,[MODELS[3],MODELS[0]])
            self.assertEqual(draft['model'],MODELS[3]);self.assertEqual(review['model'],MODELS[0])
            with self.assertRaises(LLMError) as caught:client.generate([],'another','draft')
            self.assertEqual(caught.exception.code,'local_call_limit')
            self.assertEqual(len(models),2)

    def test_switching_only_review_model_revokes_approval_fingerprint(self):
        profile=json.loads((ROOT/'data/company_U_1595.json').read_text('utf-8'))
        event=json.loads((ROOT/'data/news_snapshot.jsonl').read_text('utf-8').splitlines()[0])
        settings=Settings()
        first=context_fingerprint(settings,profile,event,date(2026,10,23),'cfo')[0]
        second=context_fingerprint(replace(settings,review_model=MODELS[0]),profile,event,date(2026,10,23),'cfo')[0]
        self.assertNotEqual(first,second)

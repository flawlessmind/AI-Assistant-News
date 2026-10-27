"""Regressions from the authorized final live cohort; model transport is never called."""
from datetime import date
import copy
import json
from pathlib import Path
import unittest
from alfa_app.agent import check_draft,repair_instructions
from alfa_app.evidence import attach_page_context,semantic_issues,source_constraints,model_evidence,unestablished_intragroup_payment
from alfa_app.agent import literal_quotes
from alfa_app.config import ROOT
from alfa_research import retrieve
from test_service import valid_draft


class FinalLiveRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.events=[json.loads(line) for line in (ROOT/'data/news_current.jsonl').read_text('utf-8').splitlines()]
        cls.profile=json.loads((ROOT/'data/company_U_83932.json').read_text('utf-8'))
        cls.event=cls.events[9]
        cls.evidence=attach_page_context(ROOT/'data/research.sqlite',retrieve(ROOT/'data/research.sqlite',
            'инфляция цены',date(2026,10,23),source_id='alfa_inflation',limit=1))

    def candidate(self,text):
        draft=valid_draft({'bank_evidence':self.evidence,'news':self.event})
        draft['risks'][0].update(text=text,evidence_ids=[self.event['event_id']],conditional=True)
        return draft

    def test_buyer_mortgage_is_not_company_floating_rate_debt(self):
        text='При сценарии, когда семейная ипотека с длительным сроком обеспечивает значимую часть продаж, рост платежа после льготного периода способен замедлить спрос на жильё и приток средств от покупателей.'
        issues=check_draft(self.candidate(text),self.profile,self.event,self.evidence,ROOT/'data/research.sqlite')
        self.assertNotIn('credit_cost_requires_contract_condition',issues)

    def test_buyer_reference_cannot_hide_unestablished_company_credit_cost(self):
        for text in ['Рост платежей покупателей по семейной ипотеке означает, что кредиты компании подорожают.',
                     'Рост платежей покупателей увеличит кредитные платежи самой компании.']:
            issues=check_draft(self.candidate(text),self.profile,self.event,self.evidence,ROOT/'data/research.sqlite')
            self.assertIn('credit_cost_requires_contract_condition',issues)

    def test_mortgage_exception_requires_the_actual_news_evidence(self):
        draft=self.candidate('Если растут платежи покупателей по семейной ипотеке, продажи требуют проверки.')
        draft['risks'][0]['evidence_ids']=[self.evidence[0]['evidence_id']]
        draft['risks'][0]['text']='При семейной ипотеке рост платежей покупателей ухудшит спрос.'
        issues=check_draft(draft,self.profile,self.event,self.evidence,ROOT/'data/research.sqlite')
        self.assertIn('credit_cost_requires_contract_condition',issues)

    def test_explicit_unknown_company_effect_is_not_an_unqualified_sales_claim(self):
        profile=json.loads((ROOT/'data/company_U_1595.json').read_text('utf-8'))
        source={'evidence_id':'scoped','text':'Непродовольственные товары, в частности автомобили.'}
        text='Если охлаждение затронет торговлю одеждой, выручка может замедлиться; данных о собственных продажах компании нет, влияние неизвестно.'
        draft={'interpretations':[{'text':text,'evidence_ids':['scoped']}],
               'scope_note':'Автомобили — исходный пример; данных о торговле одеждой компании нет.'}
        self.assertNotIn('clothing_summary_overstates_macro_evidence',semantic_issues(draft,profile,[source]))
        draft['interpretations'][0]['text']='Охлаждение спроса замедлит выручку торговли одеждой.'
        self.assertIn('clothing_summary_overstates_macro_evidence',semantic_issues(draft,profile,[source]))

    def test_real_page_month_and_segment_are_sent_as_explicit_constraints(self):
        sources=attach_page_context(ROOT/'data/research.sqlite',retrieve(ROOT/'data/research.sqlite',
            'Оборот розничной торговли июле',date(2026,10,23),source_id='alfa_labor_7m',limit=1))
        constraints=source_constraints(sources[0])
        self.assertTrue(any('июле' in item for item in constraints))
        self.assertTrue(any('автомобил' in item.lower() for item in constraints))
        self.assertEqual(model_evidence(sources,literal_quotes)[0]['text'],sources[0]['page_context'])
        self.assertEqual(model_evidence(sources,literal_quotes)[0]['usage_constraints'],constraints)
        self.assertTrue(any('месяц' in text for text in repair_instructions(['bank_observation_period_missing_or_changed'])))

    def test_a_negated_external_receipt_does_not_prove_group_payment(self):
        text='Единственный внутригрупповой перевод не является внешним поступлением.'
        self.assertTrue(unestablished_intragroup_payment(text))
        issues=semantic_issues({'points':[{'kind':'limitation','text':text}]},self.profile,[])
        self.assertIn('intragroup_payment_not_established',issues)

    def test_summary_cannot_rename_a_self_transfer(self):
        issues=semantic_issues({'summary':'Связи не показывают доли денег; внутригрупповой перевод не является внешним поступлением.'},self.profile,[])
        self.assertIn('intragroup_payment_not_established',issues)

    def test_self_transfer_and_explicit_unknown_group_payment_are_allowed(self):
        for text in ['Самоперевод — перевод компании самой себе; он исключён из внешних связей.',
                     'Самоперевод не доказывает внутригрупповой перевод.',
                     'Внутригрупповой перевод не установлен по имеющимся данным.',
                     'Проверить, есть ли внутригрупповые переводы: GROUP_ID не устанавливает их назначение.']:
            self.assertFalse(unestablished_intragroup_payment(text),text)

    def test_shared_group_id_alone_cannot_establish_a_group_payment(self):
        self.assertTrue(unestablished_intragroup_payment('Перевод внутри группы подтверждён общим GROUP_ID.'))
        self.assertTrue(any('самоперевод' in item.lower() for item in repair_instructions(['intragroup_payment_not_established'])))

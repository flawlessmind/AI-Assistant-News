"""Synthetic CSV/news regressions; these are not demonstration publications."""
import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest

from alfa_profiles import build, FILES, PRODUCTS
from alfa_app.store import Store


class AuditDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_csv(self, kind, records):
        with (self.root / FILES[kind]).open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(records[0]), delimiter=';')
            writer.writeheader()
            writer.writerows(records)

    def profile(self, payments):
        fields = ['SYNTHETIC_U', 'GROUP_ID', 'MAINOKVED_CODE', 'MAINOKVED_NAME',
                  'REGION', 'CITY', 'COMPANY_AGE_YEARS_GRADATION', 'AMOUNT_WORKERS_FNS_GRADATION',
                  'REVENUE_COMPANY', 'CL_TYPE', 'ECO_MARK_LABEL', 'FTS_FLAG', *PRODUCTS]
        company = dict.fromkeys(fields, '')
        company.update(SYNTHETIC_U='SELF', MAINOKVED_CODE='41.20', CL_TYPE='CLIENT')
        known = dict(company, SYNTHETIC_U='KNOWN')
        self.write_csv('companies', [company, known])
        self.write_csv('links', [{'SYNTHETIC_F': 'F1', 'SYNTHETIC_U': 'SELF'}])
        self.write_csv('payments', [dict(zip(['PAYER', 'BENEFICIARY', 'PAY_AMT', 'PAY_CNT'], row)) for row in payments])
        return build(self.root, 'SELF')['payments']

    def test_self_transfers_separated_and_missing_profiles_kept(self):
        result = self.profile([('SELF', 'SELF', '900', '9'), ('SELF', 'KNOWN', '60', '2'),
                               ('SELF', 'UNKNOWN', '40', '3'), ('KNOWN', 'SELF', '30', '1'),
                               ('UNKNOWN', 'SELF', '10', '1')])
        self.assertEqual(result['outgoing_amount_sum'], 1000)
        self.assertEqual(result['incoming_amount_sum'], 940)
        self.assertEqual(result['self_transfer_amount_sum'], 900)
        self.assertEqual(result['self_transfer_relation_rows'], 1)
        self.assertEqual(result['self_transfer_transaction_count_sum'], 9)
        self.assertEqual(result['outgoing_external_amount_sum'], 100)
        self.assertEqual(result['incoming_external_amount_sum'], 40)
        self.assertEqual(result['outgoing_external_relation_rows'], 2)
        self.assertEqual(result['incoming_external_relation_rows'], 2)
        self.assertEqual(result['outgoing_external_transaction_count_sum'], 5)
        self.assertEqual(result['outgoing_external_missing_profile_amount_sum'], 40)
        self.assertEqual(result['incoming_external_missing_profile_counterparty_count'], 1)
        self.assertEqual([item['company_id'] for item in result['largest_payees']], ['KNOWN', 'UNKNOWN'])
        self.assertEqual(result['largest_payees'][0]['share_of_external_amount'], .6)
        self.assertEqual(result['largest_payers'][0]['share_of_external_amount'], .75)
        self.assertTrue(result['largest_payees'][1]['profile_missing'])
        self.assertFalse(result['largest_payees'][1]['profile_available'])

    def test_top_five_shares_use_all_external_relations(self):
        result = self.profile([('SELF', f'UNKNOWN_{i}', '10', '1') for i in range(6)])
        self.assertEqual(len(result['largest_payees']), 5)
        self.assertEqual(result['outgoing_external_counterparty_count'], 6)
        self.assertAlmostEqual(result['largest_payees'][0]['share_of_external_amount'], 1/6)

    def test_self_only_has_no_external_concentration(self):
        result = self.profile([('SELF', 'SELF', '900', '9')])
        self.assertEqual(result['outgoing_external_amount_sum'], 0)
        self.assertEqual(result['largest_payees'], [])
        self.assertEqual(result['largest_payers'], [])

    def test_network_counts_unique_external_companies_and_keeps_unknown_profiles(self):
        self.profile([('SELF','SELF','900','9'),('SELF','KNOWN','60','2'),
                      ('SELF','KNOWN','20','1'),('SELF','UNKNOWN','40','3'),('KNOWN','SELF','10','1')])
        profile=build(self.root,'SELF');out=profile['network']['outgoing']
        self.assertEqual(out['unique_company_count'],2)
        self.assertEqual(out['classified_count'],1)
        self.assertEqual(out['missing_profile_count'],1)
        self.assertEqual(out['industry_divisions'],[{'okved_division':'41','company_count':1}])
        self.assertEqual(profile['network']['incoming']['unique_company_count'],1)

    def test_conflicting_counterparty_is_not_assigned_an_industry_and_group_generator_is_counted(self):
        self.profile([('SELF','KNOWN','60','2')])
        import csv
        with (self.root/FILES['companies']).open(encoding='utf-8') as stream:
            companies=list(csv.DictReader(stream,delimiter=';'))
        for company in companies:company['GROUP_ID']='group-test'
        companies.append(dict(companies[1],MAINOKVED_CODE='47.71'))
        self.write_csv('companies',companies)
        profile=build(self.root,'SELF')
        self.assertEqual(profile['network']['outgoing']['ambiguous_profile_count'],1)
        self.assertEqual(profile['network']['outgoing']['industry_divisions'],[])
        self.assertEqual(profile['network']['group']['unique_company_count'],2)
        self.assertEqual(profile['network']['group']['classified_count'],1)

    def test_zero_external_amount_has_unknown_share(self):
        result = self.profile([('SELF', 'UNKNOWN', '0', '1')])
        self.assertIsNone(result['largest_payees'][0]['share_of_external_amount'])

    def test_changed_alias_is_independent_and_seed_cannot_replace_it(self):
        store = Store(self.root / 'service.sqlite')
        original = {'event_id': 'first', 'source_url': 'https://www.cbr.ru/press/first',
                    'content_sha256': 'same-content', 'published_at': '2026-10-20T12:00:00+03:00'}
        alias = dict(original, event_id='alias-id', source_url='https://www.cbr.ru/press/second')
        self.assertEqual(store.put_event(original), ('first', 'inserted'))
        self.assertEqual(store.put_event(alias), ('first', 'duplicate'))
        revised = dict(alias, content_sha256='changed-content')
        self.assertEqual(store.put_event(revised), ('alias-id', 'inserted'))
        self.assertEqual(len(store.events()), 2)
        self.assertEqual(store.event('first')['content_sha256'], 'same-content')
        self.assertEqual(store.put_event(copy.deepcopy(alias), update_existing=False), ('alias-id', 'duplicate'))
        self.assertEqual(store.event('alias-id')['content_sha256'], 'changed-content')
        self.assertEqual(store.put_event(copy.deepcopy(revised)), ('alias-id', 'duplicate'))


if __name__ == '__main__':
    unittest.main()

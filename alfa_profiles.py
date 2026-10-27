#!/usr/bin/env python3
"""Alfa Hack case 3: build one explainable company profile from supplied CSVs.

Usage:
    python alfa_profiles.py --data-dir . --company-id U_1595 --output company_profile.json

Only Python standard library required. No LLM/API calls.
"""
import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

FILES = {
    'companies': 'df_Uid_data_hack.csv',
    'links': 'df_Fid_Uid_link_hack.csv',
    'payments': 'df_Uid_payment_hack.csv',
}
PRODUCTS = {
    'CL_PRODUCT_RKO_MARK': 'settlement_account',
    'CL_PRODUCT_GAR_MARK': 'bank_guarantee',
    'CL_PRODUCT_LOA_MARK': 'credit',
    'CL_PRODUCT_LEA_MARK': 'leasing',
    'CL_PRODUCT_DEP_MARK': 'deposit',
    'CL_PRODUCT_ZPP_MARK': 'payroll',
    'FACTORING_MARK': 'factoring',
}


def rows(path):
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        yield from csv.DictReader(f, delimiter=';')


def number(s):
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def build(data_dir, uid):
    paths = {key: data_dir / name for key, name in FILES.items()}
    for p in paths.values():
        if not p.is_file():
            raise FileNotFoundError(f'Missing file: {p}')

    target_rows = []
    group_rows = []
    known_ids = set()
    company_records = defaultdict(list)
    for c in rows(paths['companies']):
        known_ids.add(c['SYNTHETIC_U'])
        company_records[c['SYNTHETIC_U']].append(c)
        if c['SYNTHETIC_U'] == uid:
            target_rows.append(c)
    if not target_rows:
        raise ValueError(f'Company {uid} absent from company table')
    if len(target_rows) != 1:
        raise ValueError(f'Ambiguous company {uid}: {len(target_rows)} different rows; do not merge!')
    c = target_rows[0]
    group_id = c['GROUP_ID']
    if group_id:
        # Avoid counting conflicting records as distinct legal entities.
        group_rows = [r for r in rows(paths['companies']) if r['GROUP_ID'] == group_id]
        group_ids = [r['SYNTHETIC_U'] for r in group_rows]
        group_uncertain = len(set(group_ids)) != len(group_ids)
    else:
        group_uncertain = False

    outgoing_count = incoming_count = 0
    outgoing_amt = incoming_amt = 0.0
    outgoing_transactions = incoming_transactions = 0.0
    self_count = 0
    self_amt = self_transactions = 0.0
    external_out_count = external_in_count = 0
    external_out_amt = external_in_amt = 0.0
    external_out_transactions = external_in_transactions = 0.0
    counterparties_in = Counter()
    counterparties_out = Counter()
    for p in rows(paths['payments']):
        payer, beneficiary = p['PAYER'], p['BENEFICIARY']
        amount = number(p['PAY_AMT']) or 0.0
        count = number(p['PAY_CNT']) or 0.0
        if payer == uid:
            outgoing_count += 1
            outgoing_amt += amount
            outgoing_transactions += count
            if beneficiary != uid:
                external_out_count += 1
                external_out_amt += amount
                external_out_transactions += count
                counterparties_out[beneficiary] += amount
        if beneficiary == uid:
            incoming_count += 1
            incoming_amt += amount
            incoming_transactions += count
            if payer != uid:
                external_in_count += 1
                external_in_amt += amount
                external_in_transactions += count
                counterparties_in[payer] += amount
        if payer == uid and beneficiary == uid:
            self_count += 1
            self_amt += amount
            self_transactions += count

    person_ids = set()
    persons_to_companies = defaultdict(set)
    for r in rows(paths['links']):
        person, company = r['SYNTHETIC_F'], r['SYNTHETIC_U']
        if company == uid:
            person_ids.add(person)
        persons_to_companies[person].add(company)
    co_linked = set()
    for p in person_ids:
        co_linked.update(persons_to_companies[p])
    co_linked.discard(uid)

    def top(counter, external_amount):
        return [
            {'company_id': x, 'total_amount': round(v, 2), 'profile_available': x in known_ids,
             'profile_missing': x not in known_ids,
             'share_of_external_amount': v / external_amount if external_amount else None}
            for x, v in counter.most_common(5)
        ]

    bank_products = {name: c[key] == '1.0' for key, name in PRODUCTS.items()}
    group_sum = sum((number(r['REVENUE_COMPANY']) or 0) for r in group_rows) if group_rows else None
    def sector_summary(identifiers):
        identifiers=set(identifiers)
        counts=Counter();missing=ambiguous=unclassified=0
        for identifier in identifiers:
            records=company_records.get(identifier,[])
            if not records:missing+=1;continue
            if len(records)!=1:ambiguous+=1;continue
            code=records[0]['MAINOKVED_CODE'].strip()
            division=code.split('.')[0]
            if not division.isdigit():unclassified+=1;continue
            counts[division]+=1
        return {'unique_company_count':len(identifiers),
                'classified_count':sum(counts.values()),'missing_profile_count':missing,
                'ambiguous_profile_count':ambiguous,'unclassified_profile_count':unclassified,
                'industry_divisions':[{'okved_division':code,'company_count':count}
                                      for code,count in sorted(counts.items(),key=lambda row:(-row[1],row[0]))]}
    profile = {
        'company_id': uid,
        'company': {
            'industry_okved_code': c['MAINOKVED_CODE'],
            'industry_name': c['MAINOKVED_NAME'],
            'region': c['REGION'],
            'city': c['CITY'],
            'age_band': c['COMPANY_AGE_YEARS_GRADATION'],
            'employee_band_fns': c['AMOUNT_WORKERS_FNS_GRADATION'] or None,
            'revenue': number(c['REVENUE_COMPANY']),
            'customer_type': c['CL_TYPE'],
            'economic_stage': c['ECO_MARK_LABEL'] or None,
            'fts_activity_flag': c['FTS_FLAG'] == '1.0',
            'products': bank_products,
        },
        'group': {
            'group_id': group_id or None,
            'member_record_count': len(group_rows),
            'revenue_sum_of_listed_records_not_consolidated': group_sum,
            'contains_repeated_company_id': group_uncertain,
        },
        'payments': {
            'outgoing_relation_rows': outgoing_count,
            'incoming_relation_rows': incoming_count,
            'outgoing_amount_sum': round(outgoing_amt, 2),
            'incoming_amount_sum': round(incoming_amt, 2),
            'outgoing_transaction_count_sum': int(outgoing_transactions),
            'incoming_transaction_count_sum': int(incoming_transactions),
            'self_transfer_relation_rows': self_count,
            'self_transfer_amount_sum': round(self_amt, 2),
            'self_transfer_transaction_count_sum': int(self_transactions),
            'outgoing_external_relation_rows': external_out_count,
            'incoming_external_relation_rows': external_in_count,
            'outgoing_external_amount_sum': round(external_out_amt, 2),
            'incoming_external_amount_sum': round(external_in_amt, 2),
            'outgoing_external_transaction_count_sum': int(external_out_transactions),
            'incoming_external_transaction_count_sum': int(external_in_transactions),
            'outgoing_external_counterparty_count': len(counterparties_out),
            'incoming_external_counterparty_count': len(counterparties_in),
            'outgoing_external_missing_profile_counterparty_count': sum(x not in known_ids for x in counterparties_out),
            'incoming_external_missing_profile_counterparty_count': sum(x not in known_ids for x in counterparties_in),
            'outgoing_external_missing_profile_amount_sum': round(sum(v for x, v in counterparties_out.items() if x not in known_ids), 2),
            'incoming_external_missing_profile_amount_sum': round(sum(v for x, v in counterparties_in.items() if x not in known_ids), 2),
            'largest_payees': top(counterparties_out, external_out_amt),
            'largest_payers': top(counterparties_in, external_in_amt),
        },
        'person_connections': {
            'unique_person_count': len(person_ids),
            'other_companies_sharing_person_id_count': len(co_linked),
        },
        'network': {
            'outgoing':sector_summary(counterparties_out),
            'incoming':sector_summary(counterparties_in),
            'group':sector_summary(r['SYNTHETIC_U'] for r in group_rows),
            'basis':'Unique company identifiers linked by payment direction or the supplied GROUP_ID; no amount weighting.',
            'limitations':['Payment links do not establish the purpose of a transfer or supplier/customer roles.',
                           'Sector counts describe counterparties, not spending shares, revenue exposure or ownership.',
                           'Missing and conflicting profiles are excluded from sector classification and counted separately.',
                           'Companies sharing GROUP_ID are not assumed to form a consolidated or controlled group.'],
        },
        'data_quality_notes': [
            'Company identifiers are synthetic; do not identify a real business.',
            'Payments may involve companies absent from the company table.',
            'No balance, debt principal, payment purpose, market size or company market share is provided.',
            'No monetary units/observation period are specified in this CSV; verify in hackathon documentation.',
            'Group revenue is a non-consolidated arithmetic sum and may include intra-group double counting.',
            'Original incoming/outgoing totals include self-transfers; external totals and largest counterparties exclude them. Shares use the corresponding full external amount, not the top-five sum.',
            'Missing counterparty profiles are retained and explicitly marked; payment links do not establish supplier/customer roles.',
        ],
    }
    return profile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=Path('.'))
    parser.add_argument('--company-id', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = build(args.data_dir, args.company_id)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text+'\n', encoding='utf-8')
        print('Saved', args.output)
    else:
        print(text)


if __name__ == '__main__':
    main()

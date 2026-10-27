"""Explicit retrieval comparison using real local embeddings, without any LLM."""
import argparse
from datetime import date
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from alfa_app.semantic import search_signature
from alfa_research import retrieve, verify_quote

# Page targets follow the source's subject and the actual text on that physical
# page. This small engineering control is not a statistical quality estimate.
CASES = [
    ('labor_paraphrase', 'Работодателям приходится повышать оплату персонала', 'alfa_labor_7m', 2),
    ('consumer_paraphrase', 'Люди перестали так активно тратить деньги в магазинах', 'alfa_consumption', 1),
    ('rate_paraphrase', 'Центральный банк сохранил стоимость денег', 'alfa_policy_rate', 1),
    ('inflation', 'инфляция бензин недельная', 'alfa_inflation', 1),
    ('gdp', 'прогноз роста ВВП инвестиции', 'alfa_gdp_1h', 3),
    ('credit', 'Ключевая ставка пауза кредиты', 'alfa_policy_rate', 1),
    ('shopping_paraphrase', 'Покупки населения и расходы в торговых точках', 'alfa_consumption', 1),
    ('staff_paraphrase', 'Нехватка сотрудников заставляет фирмы поднимать зарплаты', 'alfa_labor_7m', 2),
    ('unrelated_food', 'Как приготовить борщ из свёклы', None, None),
    ('unrelated_physics', 'квантовая запутанность фотонов в вакууме', None, None),
    ('unrelated_computer', 'Как заменить видеокарту в ноутбуке?', None, None),
]


def evaluate():
    database = ROOT/'data/research.sqlite'
    records = []
    for name, query, source_id, page in CASES:
        results = {}
        for mode in ('lexical', 'semantic', 'hybrid'):
            rows = retrieve(database, query, date(2026, 10, 22), limit=5, mode=mode)
            ranks = [i+1 for i, row in enumerate(rows) if row['source_id'] == source_id and row['page'] == page]
            rank = min(ranks, default=None)
            exact_text = all(verify_quote(database, row['source_id'], row['page'], row['text'])[
                             'quote_present_in_extracted_text'] for row in rows)
            results[mode] = {'expected_rank':rank, 'hit_at_3':rank is not None and rank <= 3,
                             'hit_at_5':rank is not None, 'empty':not rows,
                             'all_text_on_original_page':exact_text,
                             'passed':(not rows if source_id is None else rank is not None) and exact_text,
                             'returned':[{key:row.get(key) for key in
                                 ('source_id','page','evidence_id','retrieval_method','semantic_score','query_terms_matched')}
                                 for row in rows]}
        records.append({'id':name, 'query':query, 'expected':{'source_id':source_id,'page':page},
                        'results':results})
    relevant = [row for row in records if row['expected']['source_id']]
    unrelated = [row for row in records if not row['expected']['source_id']]
    summary = {mode:{'relevant_cases':len(relevant),
                     'hit_at_3':sum(row['results'][mode]['hit_at_3'] for row in relevant),
                     'hit_at_5':sum(row['results'][mode]['hit_at_5'] for row in relevant),
                     'unrelated_cases':len(unrelated),
                     'unrelated_empty':sum(row['results'][mode]['empty'] for row in unrelated),
                     'failed':sum(not row['results'][mode]['passed'] for row in records)}
               for mode in ('lexical','semantic','hybrid')}
    return {'kind':'small_explicit_local_retrieval_comparison', 'analysis_as_of':'2026-10-22',
            'generative_requests':0, 'statistical_accuracy_estimate':False,
            'search':search_signature(database,'hybrid'), 'summary':summary, 'records':records,
            'limits':[
                '11 выбранных запросов не являются репрезентативным бенчмарком.',
                'Порог cosine 0.82 — инженерный отсев, не вероятность правильности.',
                'Порог проверялся на темах труда/потребления и двух посторонних запросах; ноутбук добавлен после его выбора.',
                'Смысловая модель может пропускать полезные источники и возвращать тематически похожие нерелевантные фрагменты.',
                'Проверено наличие текста на исходной странице; финансовые числа OCR этим не подтверждены.',
                'Позиция в первых трёх/пяти результатах не доказывает корректность итогового бизнес-вывода.'
            ]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'data/ready_search_2026-10-22.json')
    args = parser.parse_args()
    report = evaluate()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'report':str(args.output),'summary':report['summary'],'generative_requests':0},ensure_ascii=False))
    raise SystemExit(0 if not report['summary']['hybrid']['failed'] else 1)

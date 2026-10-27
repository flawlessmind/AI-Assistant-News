"""Small, explicit offline quality sample; no LLM and no accuracy estimate.

Run from the project root: python -X utf8 scripts/evaluate_quality.py
The expected source/page pairs are manually selected from actual OCR pages;
expected business behavior covers two real stories, two companies and two roles.
"""
import argparse
import copy
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from alfa_app.config import Settings
from alfa_app.agent import Agent
from alfa_app.news import canonical_url
from alfa_app.ranking import classify,rank_event
from alfa_app.store import Store
from alfa_research import retrieve,verify_quote


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def manual_news(snapshot):
    """An explicitly partial manual import, never synthetic HTML/full scraping."""
    url=canonical_url(snapshot['source_url'])
    excerpt=snapshot['verbatim_excerpt']
    text=excerpt+'\n\nРедакционный пересказ (не дословная цитата): '+snapshot['editorial_summary']
    return {'event_id':'news_'+hashlib.sha256(url.encode()).hexdigest()[:20],
            'source_url':url,'title':snapshot['source_title'],'publisher':snapshot['publisher'],
            'published_at':snapshot['published_at'],'event_date':None,
            'event_date_note':'Дата события отдельно не установлена; период наблюдений хранится отдельно.',
            'observation_period':snapshot['observation_period'],'text':text,
            'facts':[{'kind':'source_excerpt','text':excerpt,'char_start':0,'char_end':len(excerpt),
                      'semantic_fact_check':False}],
            'content_sha256':hashlib.sha256(text.encode()).hexdigest(),
            'topics':classify(snapshot['source_title'],text),
            'ingestion_method':snapshot['retrieval_method'],
            'full_article_snapshot_available':False,'is_historical_example':True,
            'is_synthetic_news':False}


def evaluate(output,as_of):
    database=ROOT/'data/research.sqlite'
    snapshot_path=ROOT/'data/news_snapshot.jsonl'
    manual_path=ROOT/'data/news_audit_2026-10-22.json'
    snapshot=[json.loads(line) for line in snapshot_path.read_text(encoding='utf-8').splitlines() if line.strip()]
    manual=read_json(manual_path)
    new_event=manual_news(manual)
    profiles={uid:read_json(ROOT/f'data/company_{uid}.json') for uid in ['U_1595','U_83932']}
    records=[]

    def record(kind,expected,observed,errors,**context):
        records.append(dict(kind=kind,expected=expected,observed=observed,errors=errors,
                            status='pass' if not errors else 'fail',**context))

    for query,source,page,why in [
        ('рынок труда не подтверждают охлаждения экономики','alfa_labor_7m',1,
         'На первой физической странице отчёта есть явная позиция о состоянии рынка труда.'),
        ('инвестиционная активность строительство июнь','alfa_gdp_1h',2,
         'Вторая физическая страница содержит разбор динамики предложения, строительства и инвестиций.'),
        ('доходность ОФЗ риторика','alfa_bond_brief',1,
         'Первая физическая страница связывает доходности ОФЗ с решением по ставке.'),
        ('ограничения предложения непродовольственные автомобили','alfa_labor_7m',1,
         'Страница нужна для сохранения сегмента, периода и оговорок при переносе розничной статистики.'),
    ]:
        rows=retrieve(database,query,as_of,limit=5)
        target=next((row for row in rows if row['source_id']==source and row['page']==page),None)
        errors=[] if target else ['expected_source_page_missing_from_top_5']
        quote_verified=False
        if target:
            quote_verified=verify_quote(database,source,page,target['text'][:120])['quote_present_in_extracted_text']
            if not quote_verified:errors.append('retrieved_text_not_found_on_expected_page')
        record('retrieval',{'source_id':source,'physical_page':page,'within_top':5},
               {'returned':[{'source_id':row['source_id'],'physical_page':row['page'],
                             'publication_date':row['publication_date'],'evidence_id':row['evidence_id']}
                            for row in rows],
                'expected_page_text_locally_verified':quote_verified},errors,query=query,selection_reason=why)

    rss_path=ROOT/manual['rss_snapshot_file']
    rss_item=next((item for item in ET.fromstring(rss_path.read_bytes()).findall('.//item')
                   if canonical_url(item.findtext('link',''))==new_event['source_url']),None)
    absent=all(event['source_url']!=new_event['source_url'] for event in snapshot)
    temporary_root=ROOT/'.cache/quality'
    temporary_root.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='alfa-quality-',dir=temporary_root) as temporary:
        settings=Settings(runtime_dir=Path(temporary),allow_live=False)
        store=Store(Path(temporary)/'service.sqlite')
        class NoLLM:
            def generate(self,*args,**kwargs):
                raise AssertionError('Quality evaluation must never call any LLM')
        agent=Agent(settings,store,NoLLM())
        first=store.put_event(copy.deepcopy(new_event))[1]
        second=store.put_event(copy.deepcopy(new_event))[1]
        changed=copy.deepcopy(new_event)
        changed['text']+='\nТестовая редакционная пометка контроля обновления; не часть публикации.'
        changed['content_sha256']=hashlib.sha256(changed['text'].encode()).hexdigest()
        third=store.put_event(changed)[1]
        errors=[]
        if not absent:errors.append('material_already_in_original_ten_story_snapshot')
        if rss_item is None:errors.append('real_publication_not_found_in_saved_official_rss')
        if [first,second,third]!=['inserted','duplicate','updated']:errors.append('manual_ingestion_dedup_or_revision_failed')
        record('new_real_material_manual_ingestion',
               {'outside_original_snapshot':True,'official_rss_provenance':True,
                'dedup_sequence':['inserted','duplicate','updated']},
               {'outside_original_snapshot':absent,'official_rss_provenance':rss_item is not None,
                'dedup_sequence':[first,second,third],'stored_events':len(store.events()),
                'raw_html_download_verified':False,'live_rss_fetch_verified':False},errors,
               source_url=new_event['source_url'],published_at=new_event['published_at'],
               source_snapshot=str(manual_path.relative_to(ROOT)),
               rss_snapshot=str(rss_path.relative_to(ROOT)),rss_snapshot_sha256=digest(rss_path),
               note='Изменённая редакционная пометка существует только в временной БД теста; исходный снимок не меняется.')

        known_event=next(event for event in snapshot if event['event_id']=='news_9f10b60aed544eaa32d4')
        for event in [known_event,new_event]:
            for uid,profile in profiles.items():
                for role in ['cfo','ceo']:
                    ranking=rank_event(profile,event,as_of,role)
                    gaps=agent.run(profile,event,as_of,role,task='missing_data')
                    sources=agent.run(profile,event,as_of,role,task='sources')
                    errors=[]
                    industry_fields={reason['company_field'] for reason in ranking['reasons']}
                    if 'company.industry_okved_code' not in industry_fields:errors.append('industry_explanation_missing')
                    if gaps['status']!='answered' or gaps.get('answer_origin')!='rules':errors.append('data_gaps_not_answered_by_rules')
                    if gaps['llm_request_attempted'] or sources['llm_request_attempted']:errors.append('unexpected_llm_attempt')
                    gap_text=json.dumps(gaps['answer'],ensure_ascii=False)
                    expected_sector='одежды' if uid=='U_1595' else 'строительного проекта'
                    if expected_sector not in gap_text:errors.append('wrong_sector_data_gaps')
                    expected_role='денежных поступлений' if role=='cfo' else ('Ассортимент' if uid=='U_1595' else 'портфель проектов')
                    if expected_role not in gap_text:errors.append('role_specific_action_missing')
                    if sources['status']!='sources_ready':errors.append('bank_sources_not_available')
                    record('company_role_news',
                           {'industry_reason_required':True,'missing_data_status':'answered',
                            'answer_origin':'rules','sector_gap_marker':expected_sector,
                            'role_action_marker':expected_role,'sources_status':'sources_ready','llm_calls':0},
                           {'relevance_level':ranking['relevance_level'],'business_score':ranking['business_score'],
                            'reasons':ranking['reasons'],'topics':ranking['event']['topics'],
                            'missing_data_status':gaps['status'],'answer_origin':gaps.get('answer_origin'),
                            'data_gap_items':gaps['answer']['items'],'sources_status':sources['status'],
                            'bank_evidence':[{'source_id':row['source_id'],'physical_page':row['page'],
                                              'publication_date':row['publication_date']}
                                             for row in sources['bank_evidence']]},errors,
                           company_id=uid,role=role,event_id=event['event_id'],
                           source_url=event['source_url'],published_at=event['published_at'])

    report={'kind':'small_explicit_offline_quality_sample','analysis_as_of':str(as_of),
            'llm_calls':0,'api_cost_rub':0,'statistical_accuracy_estimate':False,
            'inputs':{'bank_index_sha256':digest(database),'original_news_snapshot_sha256':digest(snapshot_path),
                      'new_manual_snapshot_sha256':digest(manual_path),
                      'profile_sha256':{uid:digest(ROOT/f'data/company_{uid}.json') for uid in profiles}},
            'summary':{'records':len(records),'passed':sum(record['status']=='pass' for record in records),
                       'failed':sum(record['status']=='fail' for record in records)},
            'records':records,
            'limits':['Это вручную выбранный контроль из четырёх запросов и двух реальных материалов, не статистическая точность.',
                      'Смысл всех формулировок LLM не оценивался: генеративных запросов не было.',
                      'Проверено наличие текста на странице, а не ручная верификация финансовых чисел OCR.',
                      'Новое событие импортировано вручную из официально проверенной страницы и архивного RSS; сетевой full-article loader этим не подтверждён.',
                      'Региональные наблюдения не устанавливают географию или показатели демонстрационных компаний.']}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'data/audit_quality_2026-10-22.json')
    parser.add_argument('--as-of',type=date.fromisoformat,default=date(2026,10,22))
    args=parser.parse_args()
    report=evaluate(args.output,args.as_of)
    print(json.dumps({'report':str(args.output.resolve()),**report['summary'],'llm_calls':0,
                      'statistical_accuracy_estimate':False},ensure_ascii=False))
    return 0 if not report['summary']['failed'] else 1


if __name__=='__main__':
    raise SystemExit(main())

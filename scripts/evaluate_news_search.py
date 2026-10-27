"""Reproduce six meaning-based news queries on the fixed real 40-story corpus."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from alfa_app.semantic import rank_events


def evaluate():
    events=[json.loads(line) for line in (ROOT/'data/news_current.jsonl').read_text('utf-8').splitlines()]
    controls=json.loads((ROOT/'data/news_search_cases.json').read_text('utf-8'))
    results=[]
    for case in controls:
        hits=rank_events(case['query'],events)
        expected=set(case['expected_event_ids'])
        results.append(dict(case,top5=hits[:5],passed=bool(expected&{hit['event_id'] for hit in hits[:5]})))
    return {'sample_type':'Ручные перефразирования, маленький инженерный контроль; не статистическая точность.',
            'queries':results,'passed':sum(row['passed'] for row in results),'total':len(results),'generative_calls':0}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path);args=parser.parse_args()
    report=evaluate()
    if args.output:args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    raise SystemExit(0 if report['passed']==report['total'] else 1)

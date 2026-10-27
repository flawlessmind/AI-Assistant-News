"""Build and validate a new dated bank corpus without replacing the active index."""
import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from alfa_research import build_index,manifest_entries,read_jsonl,open_database,verify_quote
from alfa_app.semantic import build_vectors,search_signature


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,default=ROOT/'data/research_manifest.json')
    parser.add_argument('--pages',type=Path,default=ROOT/'data/research_pages.jsonl')
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--pdf-dir',type=Path,help='Optional original PDF directory; verify every file against the manifest')
    args=parser.parse_args()
    destination=args.output_dir.resolve()
    if destination.exists():parser.error('Use a new directory; active indexes are never overwritten.')
    entries=manifest_entries(args.manifest)
    for entry in entries:
        if not entry['source_id'].startswith('alfa_') or not re.fullmatch('[a-f0-9]{64}',entry.get('sha256','')):
            parser.error('Each source requires an Alfa source_id and PDF SHA-256 in the manifest.')
        if args.pdf_dir:
            pdf=args.pdf_dir/entry['file_name']
            if not pdf.is_file() or hashlib.sha256(pdf.read_bytes()).hexdigest()!=entry['sha256']:
                parser.error('Original PDF hash mismatch: '+entry['file_name'])
        if not entry.get('publication_date') and not entry.get('date_note') and not entry.get('publication_date_note'):
            # Unknown dates remain excluded by retrieval; record the limitation.
            entry['update_warning']='Publication date is unknown; not current bank opinion.'
    destination.mkdir(parents=True)
    database=destination/'research.sqlite'
    report={'manifest_sha256':hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
            'pages_sha256':hashlib.sha256(args.pages.read_bytes()).hexdigest(),
            'generative_requests':0,'active_index_replaced':False,'pdf_files_verified':bool(args.pdf_dir),
            'limits':['OCR substring checks do not verify semantic support or visual numeric accuracy.']}
    report['index']=build_index(args.manifest,args.pages,database)
    report['vectors']=build_vectors(database)
    report['search']=search_signature(database,'hybrid')
    pages=read_jsonl(args.pages)
    sample=next(page for page in pages if len(page['text'])>100)
    check=verify_quote(database,sample['source_id'],sample['page'],sample['text'][:100])
    assert check['quote_present_in_extracted_text']
    connection=open_database(database)
    try:
        for row in connection.execute('SELECT metadata FROM documents'):
            metadata=json.loads(row['metadata'])
            if metadata.get('publication_date'):date.fromisoformat(metadata['publication_date'])
    finally:connection.close()
    report['passed']=True
    (destination/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    print('Activate explicitly with ALFA_RESEARCH_DATABASE='+str(database)+' and restart the server.')


if __name__=='__main__':main()

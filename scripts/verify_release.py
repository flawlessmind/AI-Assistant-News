"""Extract a release into a new directory and verify installation, tests and no-key HTTP."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import zipfile

ROOT=Path(__file__).resolve().parents[1]
FORBIDDEN={'.env','var','.venv','.git','.agents','.codex','node_modules','__pycache__','.cache','.playwright-mcp'}


def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            value.update(block)
    return value.hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive',type=Path)
    parser.add_argument('--workdir',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    archive=args.archive.resolve()
    destination=(args.workdir or ROOT/'var/release_checks'/uuid.uuid4().hex[:12]).resolve()
    if destination.exists():
        parser.error('Use a new empty destination; existing directories are never overwritten.')
    destination.mkdir(parents=True)
    with zipfile.ZipFile(archive) as package:
        names=package.namelist()
        if len(names)!=len(set(names)):
            raise ValueError('Duplicate archive names')
        for entry in package.infolist():
            relative=Path(entry.filename)
            target=(destination/relative).resolve()
            if (not target.is_relative_to(destination) or
                any(part in FORBIDDEN or part.startswith('.env.') and part!='.env.example' for part in relative.parts) or
                (entry.external_attr>>16)&0o170000==0o120000):
                raise ValueError('Unsafe or private archive entry: '+entry.filename)
        package.extractall(destination)
    manifest=json.loads((destination/'SOURCE_MANIFEST.json').read_text(encoding='utf-8'))
    if set(names)!=set(manifest)|{'SOURCE_MANIFEST.json'}:
        raise ValueError('Archive inventory differs from manifest')
    for name,expected in manifest.items():
        if digest(destination/name)!=expected:
            raise ValueError('Source hash mismatch: '+name)
    environment={key:value for key,value in os.environ.items() if not key.startswith('ALFA_')}
    environment.update(ALFA_LLM_API_KEY='',ALFA_ALLOW_LIVE_LLM='false',ALFA_SEARCH_MODE='hybrid')
    logs=destination.parent/(destination.name+'_logs')
    logs.mkdir()
    report={'archive':str(archive),'archive_sha256':digest(archive),'source_files':len(manifest),
            'manifest_verified':True,'fresh_directory':str(destination),
            'started_at':datetime.now(timezone.utc).isoformat(),'steps':[],
            'new_generative_requests':0}

    def run(name,command,timeout=300):
        result=subprocess.run(command,cwd=destination,env=environment,stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT,encoding='utf-8',errors='replace',timeout=timeout)
        (logs/(name+'.log')).write_text(result.stdout,encoding='utf-8')
        report['steps'].append({'name':name,'exit_code':result.returncode})
        print(name+': '+('OK' if result.returncode==0 else 'FAIL (see '+str(logs)+')'),flush=True)
        if result.returncode:
            raise RuntimeError(name+' failed; inspect '+str(logs/(name+'.log')))
        return result.stdout

    try:
        run('fresh_bootstrap',[sys.executable,'-X','utf8','scripts/start.py','--check'])
        python=destination/'.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        doctor=json.loads(run('doctor',[str(python),'-X','utf8','-m','alfa_app','doctor']))
        assert doctor['ready'] and not doctor['key_configured'] and not doctor['live_enabled']
        assert doctor['search']['mode']=='hybrid' and doctor['search']['chunks']==74
        report['python']=doctor['python']
        report['search']=doctor['search']
        run('pip_check',[str(python),'-m','pip','check'])
        tests=run('python_tests',[str(python),'-X','utf8','-m','unittest','discover','-s','tests'])
        report['python_tests']=int(re.search(r'Ran (\d+) tests',tests)[1])
        web=run('web_tests',['node','--test','--test-reporter=tap','tests/test_web.mjs'])
        report['web_tests']=int(re.search(r'# tests\s+(\d+)',web)[1])
        corpus=json.loads(run('corpus',[str(python),'-X','utf8','check_corpus.py']))
        assert corpus['passed']==corpus['total']
        report['corpus_checks']=corpus['passed']
        quality=json.loads(run('quality',[str(python),'-X','utf8','scripts/evaluate_quality.py','--output',str(logs/'quality.json')]))
        assert quality['failed']==0
        report['quality_checks']=quality['passed']
        acceptance=json.loads(run('expanded_acceptance',[str(python),'-X','utf8','scripts/evaluate_acceptance.py']))
        assert acceptance['failed']==0
        report['expanded_acceptance_checks']=acceptance['passed']
        news_search=json.loads(run('news_search',[str(python),'-X','utf8','scripts/evaluate_news_search.py']))
        assert news_search['passed']==news_search['total']
        report['news_search_cases']=news_search['passed']
        search=json.loads(run('search',[str(python),'-X','utf8','scripts/evaluate_search.py']))
        assert search['summary']['hybrid']['failed']==0
        report['search_comparison']=search['summary']
        http=json.loads(run('no_key_http',[str(python),'-X','utf8','scripts/verify_http.py']))
        assert http['new_llm_calls']==0 and http['no_key']
        report['http_company_role_cases']=len(http['matrix'])
        report['passed']=True
    finally:
        report['finished_at']=datetime.now(timezone.utc).isoformat()
        output=(args.output or archive.with_suffix('.verification.json')).resolve()
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print('Report: '+str(output),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())

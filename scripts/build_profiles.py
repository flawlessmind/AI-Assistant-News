"""Rebuild selected company profiles and sector graphs from the supplied CSVs."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from alfa_profiles import build,FILES


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--companies',nargs='+',default=['U_1595','U_83932','U_87','U_768','U_84'])
    args=parser.parse_args()
    if any(not re.fullmatch(r'U_\d+',uid) for uid in args.companies):parser.error('Invalid company identifier')
    before={name:hashlib.sha256((args.data_dir/name).read_bytes()).hexdigest() for name in FILES.values()}
    profiles=[]
    for uid in args.companies:
        profile=build(args.data_dir,uid)
        target=ROOT/'data'/('company_'+uid+'.json')
        target.write_text(json.dumps(profile,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        profiles.append({'company_id':uid,'industry':profile['company']['industry_okved_code'],
                         'network':profile['network'],'file_sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
        print(uid+': '+profile['company']['industry_name'],flush=True)
    after={name:hashlib.sha256((args.data_dir/name).read_bytes()).hexdigest() for name in FILES.values()}
    assert before==after
    report={'source_files_sha256':before,'raw_inputs_unchanged':True,'profiles':profiles,
            'monetary_units_and_observation_period_verified':False,'generative_requests':0}
    (ROOT/'data/profiles_provenance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':main()

"""Prepare the pinned local embedding model and corpus vectors; no generative API."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODEL = {
    'schema_version': 1,
    'model_id': 'intfloat/multilingual-e5-small',
    'revision': '614241f622f53c4eeff9890bdc4f31cfecc418b3',
    'dimension': 384, 'max_tokens': 512,
    'query_prefix': 'query: ', 'passage_prefix': 'passage: ',
    'files': {
        'model.onnx': {'source_path': 'onnx/model_qint8_avx512_vnni.onnx',
                       'size': 118346824,
                       'sha256': 'dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88'},
        'tokenizer.json': {'source_path': 'tokenizer.json', 'size': 17082730,
                           'sha256': '0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39'},
    },
}
MODEL_DIR = ROOT/'models/multilingual-e5-small'


def file_sha256(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            value.update(chunk)
    return value.hexdigest()


def download():
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name, expected in MODEL['files'].items():
        target = MODEL_DIR/name
        if (target.is_file() and target.stat().st_size == expected['size']
                and file_sha256(target) == expected['sha256']):
            print(name+': verified local copy', flush=True)
            continue
        url = ('https://huggingface.co/'+MODEL['model_id']+'/resolve/'
               +MODEL['revision']+'/'+expected['source_path'])
        temporary = target.with_suffix(target.suffix+'.part')
        size = 0
        digest = hashlib.sha256()
        request = urllib.request.Request(url, headers={'User-Agent': 'AlfaNewsLocalSearch/1.0'})
        with urllib.request.urlopen(request, timeout=45) as response, temporary.open('wb') as output:
            while True:
                chunk = response.read(1024*1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > expected['size']:
                    raise ValueError('Downloaded model file exceeds its pinned size')
                digest.update(chunk)
                output.write(chunk)
                if size % (20*1024*1024) == 0:
                    print(f'{name}: {size//1024//1024} MiB', flush=True)
        if size != expected['size'] or digest.hexdigest() != expected['sha256']:
            raise ValueError('Downloaded model file failed SHA-256 verification: '+name)
        temporary.replace(target)
        print(name+': downloaded and SHA-256 verified', flush=True)
    (MODEL_DIR/'manifest.json').write_text(json.dumps(MODEL, indent=2)+'\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download', action='store_true', help='Download public pinned model files, about 136 MB')
    parser.add_argument('--download-only', action='store_true')
    args = parser.parse_args()
    if args.download:
        download()
    if not args.download_only:
        from alfa_app.semantic import build_vectors
        print(json.dumps(build_vectors(ROOT/'data/research.sqlite'), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

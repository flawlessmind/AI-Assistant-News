"""Build a reviewable source ZIP without local configuration or runtime state."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    'alfa_profiles.py', 'alfa_research.py', 'prepare_evidence.py', 'check_corpus.py',
    'Dockerfile', 'compose.yaml', 'runtime.lock.json', '.python-version',
    'requirements.txt', 'requirements.lock.txt', 'requirements-ocr.txt', '.env.example', '.gitignore', '.dockerignore', '.gitattributes',
    'START_WINDOWS.cmd',
}
DIRECTORIES = {'alfa_app', 'web', 'tests', 'scripts', 'docs', 'data', 'models'}
EXCLUDED_PARTS = {'__pycache__', 'node_modules', 'raw', 'companies', '.git',
                  '.agents', '.codex', '.cache', '.venv', '.playwright-mcp', 'var', 'playwright-report', 'test-results'}
SUFFIXES = {'.py', '.mjs', '.js', '.css', '.html', '.md', '.json', '.jsonl', '.xml',
            '.png', '.jpg', '.pdf', '.txt', '.ps1', '.onnx'}


def release_files(root=ROOT):
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        if path.name.startswith('.env') and relative.as_posix() != '.env.example':
            continue
        if 'private' in path.name.lower() or path.name == 'apply_alfa_llm_fix.py':
            continue
        if len(relative.parts) == 1:
            allowed = path.name in ROOT_FILES or path.suffix == '.md'
        else:
            allowed = relative.parts[0] in DIRECTORIES and path.suffix in SUFFIXES
            allowed |= relative.as_posix() == 'data/research.sqlite'
            allowed |= relative.as_posix() == 'data/certificates/rosstat-ca.pem'
        if allowed:
            yield path


def build(output):
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    files = list(release_files())
    manifest = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in files}
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(ROOT).as_posix())
        archive.writestr('SOURCE_MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    return {'archive': str(output), 'files': len(files),
            'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'var/releases/alfa_service_audited_2026-10-24.zip')
    print(json.dumps(build(parser.parse_args().output), ensure_ascii=False, indent=2))

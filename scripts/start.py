"""Create an isolated local runtime, check readiness, then start the application."""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def run(command):
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode:
        raise SystemExit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8002)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--check', action='store_true', help='Prepare and check without starting HTTP')
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        parser.exit(1, 'Используйте Python 3.12; закреплённая версия проекта 3.12.14.\n')
    if not 1 <= args.port <= 65535:
        parser.error('Порт должен быть от 1 до 65535')
    environment = ROOT/'.venv'
    python = environment/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.is_file():
        print('Создаём изолированное окружение .venv…', flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    if subprocess.run([str(python), '-c', 'import pip'], cwd=ROOT,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        run([str(python), '-m', 'ensurepip', '--upgrade'])
    lock = ROOT/'requirements.lock.txt'
    if not lock.is_file():
        lock = ROOT/'requirements.txt'
    fingerprint = hashlib.sha256(lock.read_bytes()).hexdigest()
    marker = environment/'alfa-requirements.sha256'
    verify_script=('from importlib.metadata import version,PackageNotFoundError; from pathlib import Path; import sys; '
        'pins=[line.strip().split("==") for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if "==" in line]; '
        'sys.exit(0 if all(version(name)==expected for name,expected in pins) else 1)')
    matches=subprocess.run([str(python),'-c',verify_script,str(lock)],cwd=ROOT,
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
    if not matches or not marker.is_file() or marker.read_text('ascii').strip() != fingerprint:
        print('Устанавливаем закреплённые зависимости в .venv…', flush=True)
        run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(lock)])
        marker.write_text(fingerprint+'\n', encoding='ascii')
    # The ZIP already includes these files. A source checkout can restore the
    # exact public revision; valid local weights never trigger a download.
    run([str(python), '-X', 'utf8', 'scripts/setup_search.py', '--download', '--download-only'])
    if not (ROOT/'data/research.vectors.json').is_file():
        run([str(python), '-X', 'utf8', 'scripts/setup_search.py'])
    run([str(python), '-X', 'utf8', '-m', 'alfa_app', 'doctor'])
    if not args.check:
        run([str(python), '-X', 'utf8', '-m', 'alfa_app', 'serve', '--host', args.host, '--port', str(args.port)])


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)

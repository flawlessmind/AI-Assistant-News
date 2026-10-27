import argparse
import json
import sys
import signal
from importlib.metadata import version,PackageNotFoundError
from .config import ROOT, Settings
from .news import refresh
from .server import Application,make_server
from .semantic import SearchUnavailable
from .production import make_production_server,RuntimeInUse


def main():
    parser=argparse.ArgumentParser(description='Alfa case 3 local service')
    sub=parser.add_subparsers(dest='command',required=True)
    serve=sub.add_parser('serve')
    serve.add_argument('--host',default='127.0.0.1')
    serve.add_argument('--port',type=int,default=8000)
    sub.add_parser('doctor',help='Check local data, model and configuration without calling the LLM')
    refresh_parser=sub.add_parser('refresh')
    refresh_parser.add_argument('--limit',type=int,default=5)
    refresh_parser.add_argument('--export-snapshot',action='store_true')
    args=parser.parse_args()
    try:
        runtime_packages={name:version(name) for name in ('numpy','onnxruntime','tokenizers','waitress')}
    except PackageNotFoundError:
        print('Не установлены зависимости приложения. Запустите python -X utf8 scripts/start.py --check',file=sys.stderr)
        return 1
    try:
        application=Application(Settings.from_env())
    except SearchUnavailable as error:
        print('Приложение не готово к запуску: '+str(error),file=sys.stderr)
        return 1
    if args.command=='doctor':
        print(json.dumps({'ready':True,'python':sys.version.split()[0],
              'runtime_packages':runtime_packages,
              'search':application.search,'profiles':len(application.profiles),
              'news':len(application.store.events()),'key_configured':bool(application.settings.api_key),
              'live_enabled':application.settings.allow_live,'generative_requests':0},ensure_ascii=False,indent=2))
        return 0
    if args.command=='refresh':
        result=refresh(application.store,application.settings.runtime_dir/'news_raw',args.limit)
        if args.export_snapshot:
            target=ROOT/'data/news_snapshot.jsonl'
            target.write_text(''.join(json.dumps(e,ensure_ascii=False)+'\n' for e in application.store.events()),encoding='utf-8')
        (application.settings.runtime_dir/'last_refresh.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if result['feeds'] and not result['errors'] else 1
    try:
        server=make_production_server(application,args.host,args.port,own_runtime=True)
    except (RuntimeInUse,ValueError) as error:
        print(str(error),file=sys.stderr)
        return 1
    except OSError as error:
        alternative=8002 if args.port!=8002 else 8003
        print(f'Не удалось запустить сервер: адрес {args.host}:{args.port} занят или недоступен.\n'
              f'Выберите другой свободный порт, например:\n'
              f'python -X utf8 -m alfa_app serve --host {args.host} --port {alternative}\n{error}',file=sys.stderr)
        return 1
    print(f'Alfa News Insights: http://{args.host}:{server.server_port}',flush=True)
    print('Live LLM enabled: '+str(application.settings.public()['live_enabled']),flush=True)
    stopping=False
    def stop(signum,frame):
        nonlocal stopping
        application.jobs.request_stop()
        if not stopping:
            stopping=True
            raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__=='__main__':
    raise SystemExit(main())

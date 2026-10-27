"""Shared validation and safe error contract for the two HTTP transports."""
import json
import logging
from pathlib import Path
import sqlite3
import traceback
from urllib.parse import urlsplit

from .semantic import SearchUnavailable
from .store import JobQueueFull

logger = logging.getLogger(__name__)
SECURITY_HEADERS = [('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
    ('Referrer-Policy', 'strict-origin-when-cross-origin'),
    ('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")]
STATIC = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/app.js': ('app.js', 'application/javascript; charset=utf-8'),
          '/styles.css': ('styles.css', 'text/css; charset=utf-8')}


def allowed_host(host, hosts):
    parsed = urlsplit('http://' + host)
    return bool(host and not (parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment)
                and parsed.hostname in hosts and (parsed.port is None or 1 <= parsed.port <= 65535))


def allowed_origin(origin, host):
    if not origin:
        return True
    parsed = urlsplit(origin)
    return (parsed.scheme in ('http', 'https') and parsed.netloc == host
            and not (parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password))


def json_object(raw):
    def invalid_number(value):
        raise ValueError('Invalid JSON number')
    try:
        body = json.loads(raw, parse_constant=invalid_number)
    except RecursionError:
        raise ValueError('JSON nesting is too deep') from None
    if not isinstance(body, dict):
        raise ValueError('Expected object')
    return body


def failure(error, method, path):
    from .server import AnalysisInProgress
    from .jobs import JobsUnavailable,JobFailed
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return 400, {'error': 'Некорректные параметры запроса или отсутствующий объект.'}
    if isinstance(error, JobQueueFull):
        return 503, {'error': 'Очередь заданий заполнена. Дождитесь завершения текущих заданий.', 'code': 'queue_full'}
    if isinstance(error, AnalysisInProgress):
        return 409, {'error': 'Этот анализ уже выполняется; повторный вызов LLM не запущен.'}
    if isinstance(error, JobFailed):
        return 409, {'error': 'Эта попытка завершилась с ошибкой. Для новой попытки нужен новый request_id. Запрос к модели мог уже расходовать бюджет.',
                     'code': 'previous_attempt_failed'}
    if isinstance(error, SearchUnavailable):
        return 503, {'error': 'Локальный смысловой поиск недоступен. Проверьте модель и индекс командой doctor.'}
    status = 503 if isinstance(error, (OSError, sqlite3.Error, JobsUnavailable)) else 500
    # Log code locations, never exception text, query strings, bodies, prompts or keys.
    route = path if path in STATIC or path in ('/api/status', '/api/health', '/api/metrics', '/api/news',
              '/api/search', '/api/insights', '/api/jobs', '/api/refresh', '/api/feedback') else '/other'
    frames = traceback.extract_tb(error.__traceback__)
    location = ' > '.join(f'{Path(frame.filename).name}:{frame.lineno}:{frame.name}' for frame in frames[-4:])
    logger.error('HTTP %s %s failed: status=%d type=%s location=%s',
                 method if method in ('GET', 'HEAD', 'POST') else 'OTHER', route, status, type(error).__name__, location)
    if status == 503:
        return status, {'error': 'Источник, очередь или хранилище временно недоступны. История сохранена.'}
    return status, {'error': 'Внутренняя ошибка обработки. История сохранена; повторите действие или проверьте локальный журнал.'}

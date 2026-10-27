from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import ipaddress

ROOT = Path(__file__).resolve().parents[1]
MSK = timezone(timedelta(hours=3), 'Europe/Moscow')
MODELS = (
    'google/gemma-4-26b-a4b-it', 'z-ai/glm-5.3-flash', 'qwen/qwen3.6-35b-a3b',
    'deepseek/deepseek-v4.1-flash', 'qwen/qwen3-vl-30b-a3b-instruct', 'moonshotai/kimi-k2.6',
)
OFFICIAL_BASE = 'https://changellenge.up.railway.app/v1'
OFFICIAL_BASES = (OFFICIAL_BASE, 'https://api.alfahack-gateway26.ru/v1')
KEY_INFO_URL = 'https://changellenge.up.railway.app/key/info'


def require_safe_bind(host,allow_remote=False):
    try:local=ipaddress.ip_address(host).is_loopback
    except ValueError:local=host=='localhost'
    if not local and not allow_remote:
        raise ValueError('Локальное приложение не имеет авторизации. Привязка к внешнему адресу запрещена; используйте 127.0.0.1. Для защищённого развёртывания требуется явный ALFA_ALLOW_REMOTE_BIND=true.')


def now_msk():
    return datetime.now(MSK)


def budget_period(now=None):
    moment = now or now_msk()
    if moment.tzinfo is None:
        raise ValueError('Budget time must be timezone aware')
    return str((moment.astimezone(MSK) - timedelta(hours=3)).date())


def next_reset(now=None):
    moment = (now or now_msk()).astimezone(MSK)
    reset = moment.replace(hour=3, minute=0, second=0, microsecond=0)
    return (reset if moment < reset else reset + timedelta(days=1)).isoformat()


def load_env(path):
    """Only project .env, never overwrite the process environment or log values."""
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        if not line.strip() or line.lstrip().startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if key.startswith('ALFA_') and key.replace('_', '').isalnum():
            os.environ.setdefault(key, value.strip().strip('\"').strip("'"))


@dataclass
class Settings:
    base_url: str = OFFICIAL_BASE
    api_key: str = field(default='', repr=False)
    model: str = MODELS[3]
    allow_live: bool = False
    timeout: float = 120
    max_output_tokens: int = 8192
    review_max_tokens: int = 6144
    max_calls_per_period: int | None = None
    max_active_jobs: int = 16
    allow_remote_bind: bool = False
    max_rounds: int = 2
    runtime_dir: Path = ROOT / 'var'
    research_database: Path = ROOT / 'data/research.sqlite'
    search_mode: str = 'lexical'
    allowed_hosts: tuple = ('localhost','127.0.0.1','::1')
    review_model: str | None = None
    recovery_model: str = MODELS[0]

    def __post_init__(self):
        if type(self.max_active_jobs) is not int or self.max_active_jobs<1:
            raise ValueError('Maximum active jobs must be a positive integer')
        if type(self.allow_remote_bind) is not bool:raise ValueError('Invalid remote-bind setting')
        if self.model not in MODELS:
            raise ValueError('Model is not on the official six-model allowlist')
        if self.review_model is not None and self.review_model not in MODELS:
            raise ValueError('Review model is not on the official six-model allowlist')
        if self.recovery_model not in MODELS:
            raise ValueError('Recovery model is not on the official six-model allowlist')
        # Permit one conventional trailing slash, but no aliases, credentials,
        # ports, query strings, fragments or alternate paths with the key.
        normalized = self.base_url.removesuffix('/') if isinstance(self.base_url,str) else ''
        if normalized not in OFFICIAL_BASES:
            raise ValueError('Only the two official API gateways are allowed')
        self.base_url = normalized
        self.runtime_dir = Path(self.runtime_dir)
        self.research_database = Path(self.research_database)
        if self.search_mode not in ('hybrid', 'semantic', 'lexical'):
            raise ValueError('Search mode must be hybrid, semantic or lexical')
        if self.max_calls_per_period==0 and type(self.max_calls_per_period) is int:self.max_calls_per_period=None
        if self.max_calls_per_period is not None and (type(self.max_calls_per_period) is not int or self.max_calls_per_period<1):
            raise ValueError('Invalid optional local call limit')
        if (not 128 <= self.max_output_tokens <= 16384 or
                not 128 <= self.review_max_tokens <= 8192):
            raise ValueError('Invalid local request or output limit')
        if not 1 <= self.max_rounds <= 3 or not 1 <= self.timeout <= 180:
            raise ValueError('Invalid round limit or timeout')

    def output_limit(self, stage):
        return self.review_max_tokens if stage == 'review' else self.max_output_tokens

    def model_for(self,stage):
        if stage=='draft_recovery':return self.recovery_model
        return (self.review_model or self.model) if stage=='review' else self.model

    @classmethod
    def from_env(cls):
        load_env(ROOT / '.env')
        return cls(base_url=os.getenv('ALFA_LLM_BASE_URL', OFFICIAL_BASE),
                   api_key=os.getenv('ALFA_LLM_API_KEY', ''), model=os.getenv('ALFA_LLM_MODEL', MODELS[3]),
                   review_model=os.getenv('ALFA_LLM_REVIEW_MODEL',MODELS[0]),
                   recovery_model=os.getenv('ALFA_LLM_RECOVERY_MODEL',MODELS[0]),
                   allow_live=os.getenv('ALFA_ALLOW_LIVE_LLM', '').lower() == 'true',
                   timeout=float(os.getenv('ALFA_LLM_TIMEOUT_SECONDS', '120')),
                   max_output_tokens=int(os.getenv('ALFA_LLM_MAX_OUTPUT_TOKENS', '8192')),
                   review_max_tokens=int(os.getenv('ALFA_LLM_REVIEW_MAX_TOKENS', '6144')),
                   max_calls_per_period=int(os.getenv('ALFA_MAX_CALLS_PER_PERIOD', '0')),
                   max_active_jobs=int(os.getenv('ALFA_MAX_ACTIVE_JOBS','16')),
                   allow_remote_bind=os.getenv('ALFA_ALLOW_REMOTE_BIND','false').lower()=='true',
                   max_rounds=int(os.getenv('ALFA_AGENT_MAX_ROUNDS', '2')),
                   runtime_dir=Path(os.getenv('ALFA_RUNTIME_DIR', str(ROOT / 'var'))),
                   research_database=Path(os.getenv('ALFA_RESEARCH_DATABASE',str(ROOT/'data/research.sqlite'))),
                   search_mode=os.getenv('ALFA_SEARCH_MODE', 'hybrid'),
                   allowed_hosts=tuple(host.strip().lower() for host in os.getenv('ALFA_ALLOWED_HOSTS','localhost,127.0.0.1,::1').split(',') if host.strip()))

    def public(self):
        return {'base_url':self.base_url, 'model':self.model,'review_model':self.model_for('review'),
                'recovery_model':self.recovery_model,'max_length_recoveries_per_run':1,'allowed_models':list(MODELS),
                'key_configured':bool(self.api_key), 'live_enabled':self.allow_live and bool(self.api_key),
                'reset_time_msk':'03:00', 'next_reset':next_reset(), 'period':budget_period(),
                'max_calls_per_period':self.max_calls_per_period,
                'max_active_jobs':self.max_active_jobs,'allow_remote_bind':self.allow_remote_bind,
                'draft_max_tokens':self.max_output_tokens,'review_max_tokens':self.review_max_tokens,
                'timeout_seconds':self.timeout,
                'cost_per_token_known':False, 'budget_rub_per_day':500, 'search_mode':self.search_mode}

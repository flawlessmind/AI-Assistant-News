"""Bounded OpenAI-compatible HTTP client. No SDK/framework or hidden provider fallback."""
import argparse
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from .config import Settings, budget_period, next_reset, KEY_INFO_URL
from .store import Store, CallLimitError, GatewayBudgetBlocked


class LLMError(Exception):
    def __init__(self, code, message, retry_at=None, diagnostics=None):
        self.code, self.message, self.retry_at = code, message, retry_at
        self.diagnostics = diagnostics or {}
        super().__init__(message)

    def public(self):
        result = {'code':self.code,'message':self.message,'retry_at':self.retry_at}
        if self.diagnostics:
            result['diagnostics'] = self.diagnostics
        return result


def transport(method, url, headers, body, timeout):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):
            return None
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            return response.status, response.read(2_000_001)
    except urllib.error.HTTPError as error:
        return error.code, error.read(16_000)


def parse_json_reply(content):
    if not isinstance(content, str) or len(content) > 30000:
        raise ValueError('Invalid response size or content type')
    text = content.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text, count=1)
        text = re.sub(r'\s*```$', '', text, count=1)
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError('Non-finite JSON number')

    result = json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    if not isinstance(result, dict):
        raise ValueError('Expected a JSON object')
    return result


def safe_usage(response):
    source = response.get('usage') if isinstance(response, dict) else None
    if not isinstance(source, dict):
        return {}
    result = {key:source[key] for key in ['prompt_tokens','completion_tokens','total_tokens','reasoning_tokens']
              if type(source.get(key)) is int and source[key] >= 0}
    details = source.get('completion_tokens_details')
    if isinstance(details, dict) and type(details.get('reasoning_tokens')) is int and details['reasoning_tokens'] >= 0:
        result['completion_tokens_details'] = {'reasoning_tokens':details['reasoning_tokens']}
    return result


def decode_completion(raw, max_tokens):
    """Diagnose metadata only. Never publish raw response text or chain of thought."""
    diagnostics = {'http_status':200,'response_bytes':len(raw),'requested_max_tokens':max_tokens,'usage':{}}

    def fail(reason, message):
        diagnostics['reason'] = reason
        raise LLMError('invalid_response', message, diagnostics=diagnostics)

    if len(raw) > 2_000_000:
        fail('response_too_large','Ответ шлюза превысил локальный предел размера.')
    try:
        response = json.loads(raw)
    except (ValueError, RecursionError):
        fail('invalid_envelope_json','HTTP 200: тело ответа шлюза не является корректным JSON.')
    diagnostics['usage'] = safe_usage(response)
    if not isinstance(response, dict):
        fail('unexpected_envelope','HTTP 200: ожидался объект Chat Completions.')
    if response.get('error'):
        fail('gateway_error_envelope','HTTP 200: шлюз вернул объект ошибки вместо ответа модели.')
    choices = response.get('choices')
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        fail('missing_choices','HTTP 200: в ответе отсутствует корректный choices[0].')
    choice = choices[0]
    reason = choice.get('finish_reason')
    known_reasons = ['stop','length','content_filter','tool_calls','function_call','insufficient_system_resource','aborted']
    diagnostics['finish_reason'] = reason if reason in known_reasons else 'missing_or_unknown'
    message = choice.get('message')
    if not isinstance(message, dict):
        fail('missing_message','HTTP 200: в choices[0] отсутствует объект message.')
    content = message.get('content')
    diagnostics['content_type'] = ('null' if content is None else
                                   'string' if isinstance(content,str) else
                                   'array' if isinstance(content,list) else 'other')
    reasoning = message.get('reasoning_content')
    diagnostics['reasoning_chars'] = len(reasoning) if isinstance(reasoning,str) else 0
    diagnostics['content_chars'] = (len(content) if isinstance(content,str) else
                                   sum(len(part['text']) for part in content
                                       if isinstance(part,dict) and isinstance(part.get('text'),str))
                                   if isinstance(content,list) else 0)
    if reason == 'length':
        fail('output_truncated',
             'Генерация прервана по лимиту длины (finish_reason=length; max_tokens='+str(max_tokens)+
             '). JSON не принят. Распределение токенов — в безопасной диагностике; рассуждения не публикуются.')
    if reason == 'content_filter' or message.get('refusal'):
        fail('response_refused','Модель не выдала ответ: получен отказ или content_filter.')
    if reason != 'stop' or message.get('tool_calls'):
        fail('generation_not_completed','Модель не завершила обычный текстовый ответ. См. finish_reason в ходе анализа.')
    if isinstance(content,list):
        if not all(isinstance(part,dict) and part.get('type')=='text' and isinstance(part.get('text'),str) for part in content):
            fail('unsupported_content_parts','Получен неподдерживаемый формат блоков message.content.')
        content = ''.join(part['text'] for part in content)
        diagnostics['content_chars'] = len(content)
    if content is None or isinstance(content,str) and not content.strip():
        if diagnostics['reasoning_chars']:
            fail('reasoning_without_answer','В ответе есть reasoning_content, но нет итогового message.content. Рассуждения не подменяют инсайт.')
        fail('empty_content','Модель вернула пустой message.content. Готового ответа нет.')
    if not isinstance(content,str):
        fail('unsupported_content_type','Поле message.content не является текстом или массивом текстовых блоков.')
    try:
        result = parse_json_reply(content)
    except (ValueError, RecursionError) as error:
        if isinstance(error,json.JSONDecodeError):
            diagnostics.update(json_error_line=error.lineno,json_error_column=error.colno)
        fail('invalid_content_json','Итоговый message.content не содержит корректный JSON-объект. Ошибочный текст не опубликован.')
    return result, diagnostics['usage'], diagnostics


class LLMClient:
    def __init__(self, settings, store, send=transport, clock=None):
        self.settings, self.store, self.send, self.clock = settings, store, send, clock
        self.scope = hashlib.sha256(settings.api_key.encode()).hexdigest()

    def require_live(self):
        if not self.settings.api_key:
            raise LLMError('missing_key','Ключ организаторов не настроен на сервере.')
        if not self.settings.allow_live:
            raise LLMError('live_disabled','Для вызовов API установите ALFA_ALLOW_LIVE_LLM=true.')

    def generate(self, messages, context_id, stage='draft', max_tokens=None):
        self.require_live()
        limit = self.settings.output_limit(stage)
        max_tokens = limit if max_tokens is None else max_tokens
        if not 16 <= max_tokens <= limit:
            raise ValueError('Output bound exceeds local configuration')
        selected_model=self.settings.model_for(stage)
        body = {'model':selected_model, 'messages':messages, 'max_tokens':max_tokens}
        encoded = json.dumps(body,ensure_ascii=False).encode()
        if len(encoded) > 80000:
            raise LLMError('context_too_large','Контекст превышает локальный предел; сократите доказательства.')
        cache_key = hashlib.sha256(self.scope.encode()+stage.encode()+context_id.encode()+encoded).hexdigest()
        cached = self.store.cache_get(cache_key)
        if cached:
            return dict(cached, cached=True)
        # The supplied gateway instruction does not document idempotent POSTs.
        # One physical send only: even a timeout/503 may follow paid processing.
        period = budget_period(self.clock() if self.clock else None)
        try:
            call_id = self.store.reserve(self.scope,period,self.settings.max_calls_per_period,selected_model)
        except GatewayBudgetBlocked:
            raise LLMError('budget_exceeded','Бюджет шлюза исчерпан. Генерация остановлена до следующего периода.',next_reset(self.clock() if self.clock else None))
        except CallLimitError:
            raise LLMError('local_call_limit','Достигнут локальный предел запросов; это не оценка рублей.',next_reset(self.clock() if self.clock else None))
        diagnostics = {'stage':stage,'model':selected_model,'call_id':call_id,'attempt':1,
                       'requested_max_tokens':max_tokens,'request_bytes':len(encoded),
                       'message_chars':sum(len(m.get('content','')) for m in messages),
                       'timeout_seconds':self.settings.timeout,'transport_retry_performed':False}
        uncertain_message = ('Результат запроса к шлюзу неизвестен: он мог быть обработан и оплачен. '
                             'Автоматический повтор отключён. Если нажать «Перегенерировать», '
                             'новая попытка может повторно расходовать бюджет.')
        started = time.monotonic()
        try:
            status, raw = self.send('POST',self.settings.base_url+'/chat/completions',
                {'Authorization':'Bearer '+self.settings.api_key,'Content-Type':'application/json'}, encoded,self.settings.timeout)
        except (OSError, TimeoutError, urllib.error.URLError):
            diagnostics.update(elapsed_ms=round((time.monotonic()-started)*1000),request_outcome_unknown=True)
            self.store.finish_call(call_id,'network_outcome_unknown',diagnostics=diagnostics)
            raise LLMError('request_outcome_unknown',uncertain_message,diagnostics=diagnostics)
        diagnostics.update(http_status=status,elapsed_ms=round((time.monotonic()-started)*1000))
        if b'budget has been exceeded' in raw.lower():
            self.store.finish_call(call_id,'budget_exceeded',block=True,diagnostics=diagnostics)
            raise LLMError('budget_exceeded','Бюджет шлюза исчерпан; повторов до 03:00 МСК не будет.',next_reset(self.clock() if self.clock else None),diagnostics)
        if status != 200:
            ambiguous = status in (408,429) or 500 <= status <= 599
            if ambiguous:diagnostics['request_outcome_unknown']=True
            self.store.finish_call(call_id,'http_'+str(status),diagnostics=diagnostics)
            raise LLMError('request_outcome_unknown' if ambiguous else 'authentication_error' if status in (401,403) else 'gateway_error',
                           uncertain_message if ambiguous else 'Ошибка авторизации шлюза.' if status in (401,403) else 'Шлюз вернул HTTP '+str(status)+'.',diagnostics=diagnostics)
        try:
            result, usage, response_diagnostics = decode_completion(raw,max_tokens)
        except LLMError as error:
            error.diagnostics = dict(diagnostics,**error.diagnostics)
            self.store.finish_call(call_id,error.diagnostics['reason'],error.diagnostics.get('usage'),diagnostics=error.diagnostics)
            raise
        diagnostics.update(response_diagnostics)
        self.store.finish_call(call_id,'ok',usage,diagnostics=diagnostics)
        value = {'result':result,'model':selected_model,'usage':usage,'cached':False,'cost_rub':None,
                 'diagnostics':diagnostics}
        self.store.cache_set(cache_key,value)
        return value

    def key_info(self):
        self.require_live()
        try:
            # Only this primary-host endpoint is documented by the organizers;
            # selecting the alternative generation host does not invent its API.
            status, raw = self.send('GET',KEY_INFO_URL,
                                   {'Authorization':'Bearer '+self.settings.api_key},None,self.settings.timeout)
            if status != 200:
                raise LLMError('key_info_error','Маршрут /key/info вернул HTTP '+str(status)+'.')
            data = json.loads(raw)
        except (ValueError,OSError,TimeoutError,urllib.error.URLError):
            raise LLMError('key_info_error','Не удалось получить сведения шлюза.')
        # Schema and units are unknown. Never return arbitrary gateway text or secret fields.
        safe = {k:v for k,v in data.items() if isinstance(v,(int,float,bool)) and not any(x in k.lower() for x in ['key','secret','token','password'])} if isinstance(data,dict) else {}
        return {'numeric_fields_as_reported':safe,'units_verified':False,'balance_rub':None}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command',choices=['probe'])
    parser.add_argument('--live',action='store_true',help='Explicitly allow a small paid request using the configured key')
    args = parser.parse_args()
    settings = Settings.from_env()
    if not args.live:
        print(json.dumps({'configuration':settings.public(),'live_request_performed':False},ensure_ascii=False,indent=2))
        return
    settings.allow_live = True
    client = LLMClient(settings,Store(settings.runtime_dir/'service.sqlite'))
    try:
        info = client.key_info()
        answer = client.generate([{'role':'user','content':'Ответь только JSON: {"connection":"ok"}'}], 'explicit_connection_probe_v2','probe',min(512,settings.max_output_tokens))
        print(json.dumps({'key_info':info,'response':answer,'live_request_performed':True},ensure_ascii=False,indent=2))
    except LLMError as error:
        print(json.dumps({'error':error.public(),'live_request_succeeded':False},ensure_ascii=False,indent=2))
        raise SystemExit(1)


if __name__ == '__main__':
    main()

"""Bounded background work with persistent progress; no automatic generation on restart."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import threading
import time
from .news import refresh

logger=logging.getLogger(__name__)
LABELS={'route':'Определяем задачу','plan':'Планируем поиск','retrieve':'Подбираем банковские источники',
        'draft':'Формируем ответ модели','candidate_recheck':'Перепроверяем полный сохранённый черновик',
        'generation_recovery':'Модель не завершила ответ. Повторяем с компактным заданием',
        'draft_result':'Ответ модели получен','check':'Проверяем цитаты и признаки компании',
        'model_review':'Проверка обоснованности завершена','review':'Проверяем обоснованность вывода',
        'replan':'Уточняем поиск по замечаниям','revise':'Уточняем формулировки по источникам',
        'response_error':'Получена диагностируемая ошибка шлюза','stop':'Обработка завершена',
        'data_gaps':'Формируем список недостающих данных','refresh':'Обновляем публикации'}


class JobInterrupted(Exception):
    pass


class JobsUnavailable(Exception):
    pass


class JobFailed(Exception):
    pass


class Jobs:
    def __init__(self,application):
        self.app=application
        self.executor=ThreadPoolExecutor(max_workers=2,thread_name_prefix='alfa-analysis')
        self.lock=threading.Lock()
        self.closed=False

    def submit(self,body):
        request=dict(body)
        if request.get('operation','analysis') not in ('analysis','refresh'):
            raise ValueError('Unknown operation')
        if request.get('operation')=='refresh':
            limit=request.get('limit',5)
            if type(limit) is not int or not 1<=limit<=15:raise ValueError('Invalid source limit')
            request={'operation':'refresh','limit':limit}
            context='refresh:'+str(limit)
        else:
            request['operation']='analysis'
            prepared=self.app.analysis_context(request)
            request.update(role=prepared['role'],question=prepared['question'],task=prepared['task'],
                           as_of=str(prepared['as_of']),mode=prepared['mode'],
                           parent_run_id=prepared['parent']['run_id'] if prepared['parent'] else None)
            context=prepared['fingerprint']+':'+prepared['mode']
            if prepared['regenerate']:context+=':regenerate:'+prepared['request_id']
        key=hashlib.sha256(context.encode()).hexdigest()
        with self.lock:
            if self.closed:raise JobsUnavailable('Server is stopping')
            job_id,created=self.app.store.create_job(key,request,capacity=self.app.settings.max_active_jobs)
            if created:
                try:self.executor.submit(self.work,job_id)
                except RuntimeError:
                    self.app.store.update_job(job_id,state='failed',error={
                        'message':'Очередь остановлена до начала обработки. Новых вызовов модели не было.'})
                    raise JobsUnavailable('Executor rejected the job') from None
        return self.public(job_id)

    def wait(self,job_id):
        """Preserve the synchronous API shape with the same durable attempt identity."""
        while True:
            job=self.public(job_id)
            if job['state']=='done':return job.get('run') or job.get('report')
            if self.closed:raise JobsUnavailable('Server is stopping')
            if job['state']=='failed':
                if job['context']['operation']=='refresh':raise JobsUnavailable('Refresh failed')
                raise JobFailed()
            time.sleep(.05)

    def progress(self,job_id,step):
        if self.closed:raise JobInterrupted()
        stage=step.get('step','working')
        self.app.store.update_job(job_id,progress={'stage':stage,'round':step.get('round'),
            'message':step.get('message') or LABELS.get(stage,'Выполняем анализ')})

    def work(self,job_id):
        operation=None
        try:
            job=self.app.store.job(job_id)
            self.app.store.update_job(job_id,state='running')
            progress=lambda step:self.progress(job_id,step)
            request=job['request']
            operation=request['operation']
            if request['operation']=='refresh':
                result=refresh(self.app.store,self.app.settings.runtime_dir/'news_raw',limit=request['limit'],progress=progress)
                self.app.store.update_job(job_id,state='done',result={'report':result})
            else:
                run=self.app.analyse(request,progress=progress)
                result={'run_id':run['run_id']}
                if run.get('restored_from_cache'):
                    result.update(restored_from_cache=True,trace=run['trace'])
                self.app.store.update_job(job_id,state='done',result=result)
        except JobInterrupted:
            self.fail(job_id,'Сервер остановлен между этапами задания. История и полный кеш сохранены; повторите действие после запуска.')
        except Exception as error:
            # Exception text may contain credentials; only the type is logged.
            logger.error('Background job failed: %s (%s)',job_id,type(error).__name__)
            message=('Обновление публикаций не завершено. Сохранённые новости и история доступны; повторите обновление позже.'
                     if operation=='refresh' else
                     'Задание не завершено. Проверьте локальный журнал. Если модель уже получила запрос, новая попытка может расходовать бюджет повторно.')
            self.fail(job_id,message)

    def fail(self,job_id,message):
        try:self.app.store.update_job(job_id,state='failed',error={'message':message})
        except Exception as error:
            # A permanently unavailable disk cannot be repaired by retrying paid work.
            # Persistent queued/running records will be failed by recover_jobs on restart.
            logger.error('Cannot save failed job: %s (%s)',job_id,type(error).__name__)

    def public(self,job_id):
        job=self.app.store.job(job_id)
        if not job:raise ValueError('Job not found')
        request=job['request']
        response={key:job[key] for key in ('job_id','state','progress','error','created','updated')}
        response['context']={key:request.get(key) for key in
                            ('operation','company_id','event_id','role','as_of','mode','question','task',
                             'parent_run_id','regenerate','request_id')}
        if job['state']=='done' and job['result']:
            if job['result'].get('run_id'):
                response['run']=self.app.dispatch_get('/api/runs/'+job['result']['run_id'],{})
                if job['result'].get('restored_from_cache') and response['run']['status']!='stale_result':
                    response['run']=dict(response['run'],restored_from_cache=True,trace=job['result']['trace'])
            else:response['report']=job['result']['report']
        return response

    def request_stop(self):
        """Stop accepting work and starting subsequent paid stages before HTTP drains."""
        with self.lock:
            self.closed=True

    def close(self):
        self.request_stop()
        self.executor.shutdown(wait=True,cancel_futures=True)

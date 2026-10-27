import json
import hashlib
from pathlib import Path
import sqlite3
import time
import uuid
from contextlib import contextmanager
from .config import now_msk


class CallLimitError(Exception):
    pass


class GatewayBudgetBlocked(Exception):
    pass


class JobQueueFull(Exception):
    pass


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            # Concurrent first opens can fail this PRAGMA immediately despite
            # SQLite's busy timeout. Retrying only SQLITE_BUSY is safe here:
            # no application data or external request has been submitted.
            deadline=time.monotonic()+15
            while True:
                try:
                    db.execute('PRAGMA journal_mode=WAL').fetchone()
                    break
                except sqlite3.OperationalError as error:
                    if error.sqlite_errorcode!=sqlite3.SQLITE_BUSY or time.monotonic()>=deadline:raise
                    time.sleep(.05)
            db.executescript('''
              CREATE TABLE IF NOT EXISTS news(id TEXT PRIMARY KEY, url TEXT UNIQUE NOT NULL,
                  content_hash TEXT NOT NULL, payload TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS news_hash ON news(content_hash);
              CREATE TABLE IF NOT EXISTS aliases(url TEXT PRIMARY KEY, event_id TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, company_id TEXT, created TEXT, payload TEXT);
              CREATE INDEX IF NOT EXISTS runs_context ON runs(company_id,json_extract(payload,'$.context_fingerprint'),created);
              CREATE TABLE IF NOT EXISTS llm_cache(cache_key TEXT PRIMARY KEY, payload TEXT);
              CREATE TABLE IF NOT EXISTS usage(scope TEXT, period TEXT, calls INTEGER NOT NULL DEFAULT 0,
                  blocked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(scope,period));
              CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, scope TEXT, period TEXT, status TEXT,
                  model TEXT, usage TEXT, created TEXT);
              CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, context_key TEXT NOT NULL,
                  state TEXT NOT NULL, request TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '{}',
                  result TEXT, error TEXT, created TEXT NOT NULL, updated TEXT NOT NULL);
              CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_context ON jobs(context_key)
                  WHERE state IN ('queued','running');
              CREATE TABLE IF NOT EXISTS job_requests(context_key TEXT NOT NULL, request_id TEXT NOT NULL,
                  job_id TEXT NOT NULL, PRIMARY KEY(context_key,request_id));
              CREATE TABLE IF NOT EXISTS feedback(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,
                  rating TEXT NOT NULL,comment TEXT NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS feedback_run ON feedback(run_id);
            ''')
            # Schema inspection and its additive migrations must be serialized
            # when two processes initialize the same older runtime together.
            db.execute('BEGIN IMMEDIATE')
            if 'diagnostics' not in {row[1] for row in db.execute('PRAGMA table_info(calls)')}:
                db.execute("ALTER TABLE calls ADD COLUMN diagnostics TEXT NOT NULL DEFAULT '{}'")
            if 'payload' not in {row[1] for row in db.execute('PRAGMA table_info(aliases)')}:
                # Keep the actual mirror's provenance if either URL later changes.
                # Existing aliases/history are retained; unknown snapshots stay NULL.
                db.execute('ALTER TABLE aliases ADD COLUMN payload TEXT')

    @contextmanager
    def connect(self):
        connection=sqlite3.connect(self.path, timeout=15)
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def put_event(self, event, *, update_existing=True):
        """Upsert fetched news; bundled seed data must not replace runtime revisions."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._put_event(db,dict(event),update_existing=update_existing)

    def _put_event(self,db,event,*,update_existing=True):
        """All identity changes share the caller's single write transaction."""
        url=event['source_url']
        existing=db.execute('SELECT id,content_hash,payload FROM news WHERE url=?',(url,)).fetchone()
        if existing:
            if not update_existing:return existing[0],'duplicate'
            previous=json.loads(existing[2])
            updated=dict(previous,**event)
            updated['event_id']=existing[0]
            changed=any(previous.get(key)!=value for key,value in updated.items() if key!='retrieved_at')
            if updated==previous:return existing[0],'duplicate'
            db.execute('UPDATE news SET content_hash=?,payload=? WHERE id=?',
                       (updated['content_sha256'],json.dumps(updated,ensure_ascii=False),existing[0]))
            if existing[1]!=updated['content_sha256']:
                mirrors=db.execute('SELECT url,payload FROM aliases WHERE event_id=? ORDER BY url',(existing[0],)).fetchall()
                for mirror_url,raw in mirrors:
                    snapshot=json.loads(raw) if raw else previous
                    if snapshot['source_url']==mirror_url:
                        self._put_event(db,snapshot)
                    elif raw is None:
                        # A legacy alias has no independently saved source metadata.
                        # Retain the previous canonical snapshot, without inventing
                        # a mirror's title/URL/raw HTML. A later fetch can detach it.
                        db.execute('UPDATE aliases SET payload=? WHERE url=?',
                                   (json.dumps(previous,ensure_ascii=False),mirror_url))
            return existing[0],'updated' if changed else 'duplicate'
        alias=db.execute('SELECT n.id,n.content_hash FROM aliases a JOIN news n ON n.id=a.event_id WHERE a.url=?',(url,)).fetchone()
        if alias:
            if not update_existing:return alias[0],'duplicate'
            if alias[1]==event['content_sha256']:
                db.execute('UPDATE aliases SET payload=? WHERE url=?',(json.dumps(event,ensure_ascii=False),url))
                return alias[0],'duplicate'
            # The mirror now differs. It must never update the canonical row.
        db.execute('DELETE FROM aliases WHERE url=?',(url,))
        same=db.execute('SELECT id FROM news WHERE content_hash=? ORDER BY id LIMIT 1',(event['content_sha256'],)).fetchone()
        if same:
            db.execute('INSERT INTO aliases(url,event_id,payload) VALUES(?,?,?)',
                       (url,same[0],json.dumps(event,ensure_ascii=False)))
            return same[0],'duplicate'
        identity=event['event_id']
        if db.execute('SELECT 1 FROM news WHERE id=?',(identity,)).fetchone():
            # Old callers may still carry the canonical ID of a former alias.
            identity='news_'+hashlib.sha256(url.encode()).hexdigest()[:20]
            if db.execute('SELECT 1 FROM news WHERE id=?',(identity,)).fetchone():
                raise ValueError('Conflicting news identity')
        event=dict(event,event_id=identity)
        db.execute('INSERT INTO news VALUES(?,?,?,?)',
                   (identity,url,event['content_sha256'],json.dumps(event,ensure_ascii=False)))
        return identity,'inserted'

    def events(self):
        with self.connect() as db:
            rows = db.execute('SELECT payload FROM news').fetchall()
        return sorted([json.loads(r[0]) for r in rows], key=lambda x:x['published_at'], reverse=True)

    def apply_event_revision(self,event,previous_hash):
        """Compare-and-swap a known bundled revision; never replace newer fetched news."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT url,content_hash FROM news WHERE id=?',(event['event_id'],)).fetchone()
            if not row or row!=(event['source_url'],previous_hash):return 0
            self._put_event(db,dict(event))
            return 1

    def event(self, event_id):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM news WHERE id=?', (event_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def event_for_url(self,url):
        with self.connect() as db:
            row=db.execute('SELECT payload,NULL FROM news WHERE url=? UNION ALL '
                'SELECT n.payload,a.payload FROM aliases a JOIN news n ON n.id=a.event_id WHERE a.url=? LIMIT 1',(url,url)).fetchone()
        if not row:return None
        canonical=json.loads(row[0])
        if row[1] and json.loads(row[1])['content_sha256']!=canonical['content_sha256']:
            # The pre-upgrade mirror has no verified independent metadata.
            # Its old snapshot is retained, but a fresh fetch is required.
            return None
        return canonical

    def create_job(self,context_key,request,capacity=16):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if request.get('request_id'):
                previous=db.execute('SELECT job_id FROM job_requests WHERE context_key=? AND request_id=?',
                                    (context_key,request['request_id'])).fetchone()
                if previous:return previous[0],False
                previous=db.execute("SELECT id FROM jobs WHERE context_key=? AND json_extract(request,'$.request_id')=? ORDER BY created DESC LIMIT 1",
                                    (context_key,request['request_id'])).fetchone()
                if previous:
                    db.execute('INSERT INTO job_requests VALUES(?,?,?)',(context_key,request['request_id'],previous[0]))
                    return previous[0],False
            existing=db.execute("SELECT id FROM jobs WHERE context_key=? AND state IN ('queued','running')",(context_key,)).fetchone()
            if existing:
                if request.get('request_id'):
                    db.execute('INSERT INTO job_requests VALUES(?,?,?)',(context_key,request['request_id'],existing[0]))
                return existing[0],False
            count=db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
            if capacity is not None and count>=capacity:raise JobQueueFull('Analysis queue is full')
            job_id=uuid.uuid4().hex
            moment=now_msk().isoformat()
            db.execute('INSERT INTO jobs(id,context_key,state,request,created,updated) VALUES(?,?,?,?,?,?)',
                (job_id,context_key,'queued',json.dumps(request,ensure_ascii=False),moment,moment))
            if request.get('request_id'):
                db.execute('INSERT INTO job_requests VALUES(?,?,?)',(context_key,request['request_id'],job_id))
        return job_id,True

    def job(self,job_id):
        with self.connect() as db:
            row=db.execute('SELECT id,context_key,state,request,progress,result,error,created,updated FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not row:return None
        result=dict(zip(('job_id','context_key','state','request','progress','result','error','created','updated'),row))
        for key in ('request','progress','result','error'):
            result[key]=json.loads(result[key]) if result[key] else None
        return result

    def update_job(self,job_id,state=None,progress=None,result=None,error=None):
        values={'updated':now_msk().isoformat()}
        if state is not None:values['state']=state
        for key,value in (('progress',progress),('result',result),('error',error)):
            if value is not None:values[key]=json.dumps(value,ensure_ascii=False)
        with self.connect() as db:
            db.execute('UPDATE jobs SET '+','.join(key+'=?' for key in values)+' WHERE id=?',(*values.values(),job_id))

    def recover_jobs(self):
        """Called only by a server that holds the exclusive runtime lock."""
        error=json.dumps({'message':'Сервер перезапущен до завершения задания. Ответы и кеш сохранены. Запрос к модели мог уже расходовать бюджет; новая попытка может быть оплачена повторно.'},ensure_ascii=False)
        with self.connect() as db:
            db.execute("UPDATE calls SET status='interrupted_outcome_unknown',diagnostics="
                       "json_set(COALESCE(diagnostics,'{}'),'$.request_outcome_unknown',json('true'),"
                       "'$.interrupted_by_restart',json('true')) WHERE status='reserved'")
            return db.execute("UPDATE jobs SET state='failed',error=?,updated=? WHERE state IN ('queued','running')",
                              (error,now_msk().isoformat())).rowcount

    def save_feedback(self,feedback_id,run_id,rating,comment):
        with self.connect() as db:
            existing=db.execute('SELECT run_id FROM feedback WHERE id=?',(feedback_id,)).fetchone()
            if existing and existing[0]!=run_id:raise ValueError('Feedback id belongs to a different result')
            moment=now_msk().isoformat()
            db.execute('INSERT INTO feedback VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                       'rating=excluded.rating,comment=excluded.comment,updated=excluded.updated',
                       (feedback_id,run_id,rating,comment,moment,moment))
        return {'feedback_id':feedback_id,'saved':True,'rating':rating}

    def metrics(self,scope,period):
        with self.connect() as db:
            calls=db.execute('SELECT status,usage,diagnostics FROM calls WHERE scope=? AND period=?',(scope,period)).fetchall()
            usage=db.execute('SELECT calls,blocked FROM usage WHERE scope=? AND period=?',(scope,period)).fetchone()
            jobs=dict(db.execute('SELECT state,COUNT(*) FROM jobs GROUP BY state').fetchall())
            feedback=dict(db.execute('SELECT rating,COUNT(*) FROM feedback GROUP BY rating').fetchall())
            runs=db.execute('SELECT payload FROM runs').fetchall()
        statuses={}
        for row in runs:
            status=json.loads(row[0]).get('status','unknown')
            statuses[status]=statuses.get(status,0)+1
        durations=[json.loads(row[2]).get('elapsed_ms') for row in calls]
        durations=[value for value in durations if isinstance(value,(float,int))]
        return {'period':period,'physical_attempts':usage[0] if usage else 0,
                'gateway_budget_blocked':bool(usage[1]) if usage else False,'cost_rub':None,
                'calls_by_status':{key:sum(row[0]==key for row in calls) for key in sorted({row[0] for row in calls})},
                'mean_llm_request_ms':round(sum(durations)/len(durations)) if durations else None,
                'runs_by_status':statuses,'jobs_by_state':jobs,'feedback_by_rating':feedback}

    def save_run(self, run):
        run.setdefault('run_id', uuid.uuid4().hex)
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO runs VALUES(?,?,?,?)', (run['run_id'],run['company_id'],
                       now_msk().isoformat(),json.dumps(run,ensure_ascii=False)))
        return run

    def run(self, run_id):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM runs WHERE id=?', (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def cache_get(self, key):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM llm_cache WHERE cache_key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def cache_set(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO llm_cache VALUES(?,?)', (key,json.dumps(value,ensure_ascii=False)))

    def cached_response_for_call(self,call_id):
        """Internal untrusted draft lookup; callers must validate and review again."""
        if not isinstance(call_id,str) or len(call_id)!=32:return None
        with self.connect() as db:
            rows=db.execute('SELECT payload FROM llm_cache').fetchall()
        for row in rows:
            response=json.loads(row[0])
            if response.get('diagnostics',{}).get('call_id')==call_id:return response
        return None

    def current_result(self,company_id,context_fingerprint):
        with self.connect() as db:
            rows=db.execute("SELECT id,payload FROM runs WHERE company_id=? AND json_extract(payload,'$.context_fingerprint')=? "
                            "AND json_extract(payload,'$.status') IN ('complete','answered') ORDER BY created DESC",
                            (company_id,context_fingerprint)).fetchall()
        for run_id,payload in rows:
            run=json.loads(payload)
            if (run.get('context_fingerprint')==context_fingerprint
                    and (run.get('status')=='complete' and run.get('insight') or
                         run.get('status')=='answered' and run.get('answer'))):
                return run
        return None

    def current_main_run_id(self,company_id,context_fingerprint):
        run=self.current_result(company_id,context_fingerprint)
        return run['run_id'] if run and run['status']=='complete' and not run.get('question') else None

    def main_analysis_states(self,company_id,fingerprints):
        """One SQL query for the latest verified result and real attempt per context."""
        if not fingerprints:return {}
        placeholders=','.join('?' for _ in fingerprints)
        sql="""WITH candidates AS (
            SELECT id,created,json_extract(payload,'$.context_fingerprint') AS fp,
                json_extract(payload,'$.status') AS status,
                (json_extract(payload,'$.status')='complete' AND json_type(payload,'$.insight')='object'
                 AND json_extract(payload,'$.insight')!='{}') AS verified,
                (json_extract(payload,'$.llm_request_attempted')=1 OR json_extract(payload,'$.runtime_llm_used')=1
                 OR json_extract(payload,'$.regeneration_requested')=1 OR json_extract(payload,'$.status')='complete') AS attempted
            FROM runs WHERE company_id=? AND json_extract(payload,'$.context_fingerprint') IN ("""+placeholders+""")
                AND COALESCE(json_extract(payload,'$.question'),'')=''
                AND json_extract(payload,'$.status') IN ('complete','validation_failed','generation_unavailable','insufficient_evidence')
        ), ranked AS (
            SELECT *,ROW_NUMBER() OVER (PARTITION BY fp,verified ORDER BY created DESC,id DESC) AS rn
            FROM candidates WHERE verified OR attempted
        ) SELECT id,fp,status,verified FROM ranked WHERE rn=1 ORDER BY created DESC,id DESC"""
        with self.connect() as db:rows=db.execute(sql,(company_id,*fingerprints)).fetchall()
        result={}
        for run_id,fp,status,verified in rows:
            state=result.setdefault(fp,{'cached_run_id':None,'latest_run_id':run_id,'latest_status':status})
            if verified:state['cached_run_id']=run_id
        return result

    def latest_analysis_result(self,company_id,context_fingerprint):
        with self.connect() as db:
            row=db.execute("SELECT payload FROM runs WHERE company_id=? AND json_extract(payload,'$.context_fingerprint')=? "
                "AND json_extract(payload,'$.status') IN ('complete','validation_failed','generation_unavailable','insufficient_evidence') "
                "AND (json_extract(payload,'$.llm_request_attempted')=1 OR json_extract(payload,'$.runtime_llm_used')=1 "
                "OR json_extract(payload,'$.regeneration_requested')=1 OR json_extract(payload,'$.status')='complete') "
                "ORDER BY created DESC LIMIT 1",(company_id,context_fingerprint)).fetchone()
        return json.loads(row[0]) if row else None

    def draft_candidate(self,company_id,event_id,role,as_of,question,task):
        with self.connect() as db:
            rows=db.execute('SELECT payload FROM runs WHERE company_id=? ORDER BY created DESC LIMIT 40',(company_id,)).fetchall()
        for row in rows:
            run=json.loads(row[0])
            if (run.get('status') in ('generation_unavailable','validation_failed')
                and run.get('event',{}).get('event_id')==event_id and run.get('role')==role
                and run.get('analysis_as_of')==str(as_of) and run.get('question','')==question
                and run.get('task')==task and any(step.get('step')=='draft_result' for step in run.get('trace',[]))):
                return run
        return None

    def reserve(self, scope, period, cap, model):
        call_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO usage(scope,period) VALUES(?,?)', (scope,period))
            calls, blocked = db.execute('SELECT calls,blocked FROM usage WHERE scope=? AND period=?', (scope,period)).fetchone()
            if blocked:
                raise GatewayBudgetBlocked('Gateway budget exhausted for this period')
            if cap is not None and calls >= cap:
                raise CallLimitError('Local call cap reached')
            db.execute('UPDATE usage SET calls=calls+1 WHERE scope=? AND period=?', (scope,period))
            db.execute('INSERT INTO calls(id,scope,period,status,model,usage,created) VALUES(?,?,?,?,?,?,?)',
                       (call_id,scope,period,'reserved',model,'{}',now_msk().isoformat()))
        return call_id

    def finish_call(self, call_id, status, usage=None, block=False, diagnostics=None):
        with self.connect() as db:
            db.execute('UPDATE calls SET status=?,usage=?,diagnostics=? WHERE id=?',
                       (status,json.dumps(usage or {}),json.dumps(diagnostics or {}),call_id))
            if block:
                db.execute('UPDATE usage SET blocked=1 WHERE (scope,period)=(SELECT scope,period FROM calls WHERE id=?)', (call_id,))

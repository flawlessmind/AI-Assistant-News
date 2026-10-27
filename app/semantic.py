"""Local multilingual E5 embeddings. No network, generation, or document uploads."""
from collections import OrderedDict
from functools import lru_cache
import hashlib
import json
import re
from pathlib import Path
import sqlite3
import threading
from .file_identity import sha256_file,file_signature

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT/'models/multilingual-e5-small'
MODEL_ID = 'intfloat/multilingual-e5-small'
REVISION = '614241f622f53c4eeff9890bdc4f31cfecc418b3'
FILES = {
    'model.onnx': 'dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88',
    'tokenizer.json': '0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39',
}
DIMENSION = 384
MAX_TOKENS = 512
# An engineering cutoff, checked on a small set of relevant/unrelated queries.
# Cosine similarity is never a probability or proof that a conclusion is true.
MIN_COSINE = 0.82
POLICY_VERSION = 'e5-int8-mean-pool-hybrid-v1'
RANKING_POLICY_VERSION = 'strong-lexical-union-v2'
_ENCODER = None
_ENCODER_LOCK = threading.Lock()


class SearchUnavailable(RuntimeError):
    pass


def vectors_path(database):
    return Path(database).with_suffix('.vectors.json')


class Encoder:
    def __init__(self):
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as error:
            raise SearchUnavailable('Установите зависимости: python -m pip install -r requirements.txt') from error
        for name, expected in FILES.items():
            path = MODEL_DIR/name
            if not path.is_file() or sha256_file(path) != expected:
                raise SearchUnavailable('Модель поиска отсутствует или повреждена. Выполните python scripts/setup_search.py --download')
        self.np = np
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        try:
            self.session = ort.InferenceSession(str(MODEL_DIR/'model.onnx'), sess_options=options,
                                               providers=['CPUExecutionProvider'])
            self.tokenizer = Tokenizer.from_file(str(MODEL_DIR/'tokenizer.json'))
        except Exception as error:
            raise SearchUnavailable('Не удалось загрузить локальную модель. Проверьте зависимости и модель командой doctor.') from error
        self.tokenizer.enable_truncation(max_length=MAX_TOKENS)
        self.tokenizer.enable_padding(pad_id=1, pad_token='<pad>')
        self.input_names = {item.name for item in self.session.get_inputs()}
        self.lock = threading.Lock()
        self.cache = OrderedDict()
        self.signature = hashlib.sha256(json.dumps({'id': MODEL_ID, 'revision': REVISION,
                    'files': FILES, 'policy': POLICY_VERSION}, sort_keys=True).encode()).hexdigest()

    def encode(self, texts, kind='query'):
        if kind not in ('query', 'passage'):
            raise ValueError('Unknown embedding task')
        if not texts:
            return self.np.empty((0, DIMENSION), dtype=self.np.float32)
        result = []
        with self.lock:
            for text in texts:
                if not isinstance(text, str) or not text.strip():
                    raise ValueError('Embedding text must be nonempty')
                key = (kind, hashlib.sha256(text.encode()).hexdigest())
                if key in self.cache:
                    vector = self.cache.pop(key)
                    self.cache[key] = vector
                    result.append(vector)
                    continue
                encoded = self.tokenizer.encode(kind+': '+text[:16000])
                inputs = {
                    'input_ids': self.np.array([encoded.ids], dtype=self.np.int64),
                    'attention_mask': self.np.array([encoded.attention_mask], dtype=self.np.int64),
                    'token_type_ids': self.np.zeros((1, len(encoded.ids)), dtype=self.np.int64),
                }
                output = self.session.run(None, {name: inputs[name] for name in self.input_names})[0]
                if output.ndim != 3 or output.shape[-1] != DIMENSION:
                    raise SearchUnavailable('Неожиданный формат выхода модели поиска.')
                mask = inputs['attention_mask'][..., None].astype(self.np.float32)
                vector = (output*mask).sum(axis=1)[0]/mask.sum(axis=1)[0]
                norm = self.np.linalg.norm(vector)
                if not self.np.isfinite(vector).all() or norm <= 0:
                    raise SearchUnavailable('Модель поиска вернула некорректный вектор.')
                vector = (vector/norm).astype(self.np.float32)
                self.cache[key] = vector
                while len(self.cache) > 192:
                    self.cache.popitem(last=False)
                result.append(vector)
        return self.np.stack(result)


def encoder():
    global _ENCODER
    if _ENCODER is None:
        with _ENCODER_LOCK:
            if _ENCODER is None:
                _ENCODER = Encoder()
    return _ENCODER


def build_vectors(database):
    database = Path(database)
    engine = encoder()
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        records = connection.execute('SELECT c.chunk_id,c.text,d.metadata FROM chunks c '
                    'JOIN documents d ON d.source_id=c.source_id WHERE c.searchable=1 ORDER BY c.chunk_id').fetchall()
    finally:
        connection.close()
    texts = [json.loads(row['metadata']).get('title', '')+'\n'+row['text'] for row in records]
    vectors = engine.encode(texts, kind='passage')
    payload = {'schema_version': 1, 'model_id': MODEL_ID, 'revision': REVISION,
               'encoder_signature': engine.signature, 'dimension': DIMENSION,
               'corpus_sha256': sha256_file(database), 'policy_version': POLICY_VERSION,
               'vectors': {row['chunk_id']: vector.tolist() for row, vector in zip(records, vectors)}}
    path = vectors_path(database)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':'))+'\n', encoding='utf-8')
    temporary.replace(path)
    _load_vectors.cache_clear()
    return {'path': str(path), 'chunks': len(records), 'dimension': DIMENSION,
            'model_id': MODEL_ID, 'revision': REVISION, 'llm_calls': 0}


@lru_cache(maxsize=4)
def _load_vectors(path, index_identity, corpus_hash, expected_ids):
    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
        engine = encoder()
        if (payload['corpus_sha256'] != corpus_hash or payload['encoder_signature'] != engine.signature
                or payload['dimension'] != DIMENSION or payload['schema_version'] != 1
                or payload['model_id'] != MODEL_ID or payload['revision'] != REVISION
                or payload['policy_version'] != POLICY_VERSION
                or set(payload['vectors']) != set(expected_ids)):
            raise ValueError('Index version mismatch')
        vectors = {key: engine.np.array(value, dtype=engine.np.float32) for key, value in payload['vectors'].items()}
        if not vectors or any(vector.shape != (DIMENSION,) or not engine.np.isfinite(vector).all()
                              or abs(float(engine.np.linalg.norm(vector))-1) > 0.001 for vector in vectors.values()):
            raise ValueError('Invalid vectors')
        return vectors, sha256_file(path)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SearchUnavailable('Индекс поиска отсутствует, повреждён или устарел. Выполните python scripts/setup_search.py') from error


@lru_cache(maxsize=4)
def _searchable_ids(database,identity):
    connection=sqlite3.connect(database)
    try:return tuple(row[0] for row in connection.execute('SELECT chunk_id FROM chunks WHERE searchable=1 ORDER BY chunk_id'))
    finally:connection.close()


def index_vectors(database):
    path = vectors_path(database)
    if not path.is_file():
        raise SearchUnavailable('Нет смыслового индекса. Выполните python scripts/setup_search.py')
    database=Path(database).resolve()
    expected_ids=_searchable_ids(str(database),file_signature(database))
    return _load_vectors(str(path.resolve()),file_signature(path),sha256_file(database),expected_ids)


def search_signature(database, mode):
    if mode == 'lexical':
        return {'mode': mode, 'signature': 'fts5-coverage-bm25-v1'}
    vectors, index_hash = index_vectors(database)
    return {'mode': mode, 'model_id': MODEL_ID, 'revision': REVISION,
            'encoder_signature': encoder().signature, 'index_sha256': index_hash,
            'chunks': len(vectors), 'dimension': DIMENSION, 'min_cosine': MIN_COSINE,
            'policy_version': POLICY_VERSION, 'ranking_policy_version':RANKING_POLICY_VERSION,
            'network_at_query_time': False}


def lexical_signal(query,text):
    """Require every meaningful query term; an isolated OR hit is not a fallback."""
    from alfa_research import query_terms
    terms=query_terms(query)
    normalized=lambda value:' '.join(value.casefold().replace('ё','е').split())
    needle,haystack=normalized(query),normalized(text)
    tokens=re.findall(r'[^\W_]+',haystack,re.UNICODE)
    exact=bool(needle and re.search(r'(?<!\w)'+re.escape(needle)+r'(?!\w)',haystack))
    complete=bool(terms) and all(any(token.startswith(term.strip('"*')) if term.endswith('*') else token==term.strip('"*')
                                   for token in tokens) for term in terms)
    return bool(terms) and (exact or complete),exact


def rank_chunks(database, query, records, lexical_order, mode):
    if mode not in ('semantic', 'hybrid'):
        raise ValueError('Unknown search mode')
    vectors, _ = index_vectors(database)
    if any(row['chunk_id'] not in vectors for row in records):
        raise SearchUnavailable('Смысловой индекс не соответствует фрагментам корпуса; пересоберите его.')
    engine = encoder()
    query_vector = engine.encode([query])[0]
    scores = {row['chunk_id']: float(vectors[row['chunk_id']] @ query_vector) for row in records}
    dense = sorted((row for row in records if scores[row['chunk_id']] >= MIN_COSINE),
                   key=lambda row: (-scores[row['chunk_id']], row['chunk_id']))
    dense_ranks = {row['chunk_id']: index+1 for index, row in enumerate(dense)}
    lexical_ranks = {key: index+1 for index, key in enumerate(lexical_order)}
    signals={row['chunk_id']:lexical_signal(query,row['text']) for row in records} if mode=='hybrid' else {}
    candidates=[row for row in records if row['chunk_id'] in dense_ranks or
                mode=='hybrid' and row['chunk_id'] in lexical_ranks and signals[row['chunk_id']][0]]
    ranked = []
    for row in candidates:
        key = row['chunk_id']
        score = 1/(30+dense_ranks[key]) if key in dense_ranks else 0
        if mode == 'hybrid' and key in lexical_ranks:
            strong,exact=signals[key]
            score += (1 if strong else .15)/(30+lexical_ranks[key])
            if exact:score += .01
        ranked.append(dict(row, semantic_score=round(scores[key], 6),
                           semantic_rank=dense_ranks.get(key), lexical_rank=lexical_ranks.get(key),
                           hybrid_score=score, retrieval_method=mode))
    return sorted(ranked, key=lambda row: (-row['hybrid_score'], row['chunk_id']))


def fingerprint_part(database, mode):
    """Data identity without inference; also usable for stale-result checks."""
    path = vectors_path(database)
    return {'mode': mode, 'model_id': MODEL_ID if mode != 'lexical' else None,
            'revision': REVISION if mode != 'lexical' else None,
            'policy': POLICY_VERSION, 'ranking_policy':RANKING_POLICY_VERSION, 'min_cosine': MIN_COSINE,
            'vectors_sha256': sha256_file(path) if mode != 'lexical' and path.is_file() else None}


def rank_events(query, events, mode='hybrid'):
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise ValueError('Поисковый запрос должен содержать от 1 до 500 символов.')
    if mode not in ('semantic','hybrid'):raise ValueError('Unknown search mode')
    if not events:
        return []
    engine = encoder()
    query_vector = engine.encode([query])[0]
    vectors = engine.encode([event['title']+'\n'+event.get('text', '') for event in events], kind='passage')
    ranked = []
    for event, vector in zip(events, vectors):
        score = float(vector @ query_vector)
        strong,exact=lexical_signal(query,event['title']+' '+event.get('text',''))
        if mode!='hybrid':strong=False
        if score >= MIN_COSINE or strong:
            ranked.append({'event_id': event['event_id'], 'semantic_score': round(score, 6),
                           'score': max(score,MIN_COSINE if strong else score)+(0.015 if exact and mode == 'hybrid' else 0),
                           'retrieval_method': mode, 'exact_match': exact})
    return sorted(ranked, key=lambda row: (-row['score'], row['event_id']))

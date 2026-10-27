"""Bounded, thread-safe file hashes invalidated by file identity and metadata."""
from collections import OrderedDict
import hashlib
from pathlib import Path
import threading

_HASHES=OrderedDict()
_LOCK=threading.RLock()


def file_signature(path):
    stat=Path(path).stat()
    return (stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)


def sha256_file(path):
    path=Path(path).resolve()
    with _LOCK:
        signature=file_signature(path)
        cached=_HASHES.get(path)
        if cached and cached[0]==signature:
            _HASHES.move_to_end(path)
            return cached[1]
        for _ in range(3):
            digest=hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
            after=file_signature(path)
            if after==signature:
                value=digest.hexdigest()
                _HASHES[path]=(signature,value)
                _HASHES.move_to_end(path)
                while len(_HASHES)>64:_HASHES.popitem(last=False)
                return value
            signature=after
        raise OSError('File changed during hashing; retry after the update finishes')


def clear_hash_cache():
    with _LOCK:_HASHES.clear()

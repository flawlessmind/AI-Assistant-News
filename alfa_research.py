#!/usr/bin/env python3
"""Alfa case 3, step 2: local PDF/OCR ingestion and evidence retrieval.

Search supports local E5 embeddings and SQLite FTS5/BM25; no embedding API is used.
The lexical default preserves the original CLI/library contract; the app selects hybrid.
An OCR match is not a verified financial fact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parent
STOPWORDS = set("и в во на по для с со к ко из от до о об а но или что это как не у за при мы он она они the of and to in a is for".split())
STOPWORDS.update('ли сейчас стоит так приходится всё все ещё очень какой какая какие каких почему этом этого этой нужно нужен нужны можно'.split())
# Explicit prefix expansion, not a semantic embedding model or a full stemmer.
PREFIXES = (
    "ставк", "кредит", "потреблен", "спрос", "инфляц", "зарплат", "рознич",
    "труд", "золот", "металл", "валют", "облигац", "доходност", "эконом",
    "финанс", "строит", "импорт", "экспорт", "инвест", "ввп", "нефт", "сталь",
    "ключев", "пауз", "прогноз", "снижен",
)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def manifest_entries(path: Path):
    entries = read_json(path)
    ids = [item["source_id"] for item in entries]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate source_id in manifest")
    for item in entries:
        if Path(item["file_name"]).name != item["file_name"]:
            raise ValueError("Manifest file_name must be a basename")
        if item.get("publication_date"):
            date.fromisoformat(item["publication_date"])
    return entries


def run_checked(args: list[str]):
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed: {result.stderr[-1600:]} {result.stdout[-800:]}")


def extract_pages(pdf_dir: Path, manifest_path: Path, output: Path, cache_dir: Path,
                  engine: str = "auto", scale: int = 2600, pdftoppm: str = "pdftoppm"):
    from pypdf import PdfReader  # Needed for ingestion only, not search/index.
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    entries = manifest_entries(manifest_path)
    all_pages = []
    chosen_engine = ("windows" if os.name == "nt" else "tesseract") if engine == "auto" else engine
    for item in entries:
        source = pdf_dir / item["file_name"]
        content = source.read_bytes()
        if item.get("expected_size_bytes") and len(content) != item["expected_size_bytes"]:
            raise ValueError(f"Unexpected PDF size: {source.name}")
        pdf_hash = sha256(content)
        if item.get("sha256") and item["sha256"] != pdf_hash:
            raise ValueError(f"PDF hash differs from reviewed source: {source.name}")
        reader = PdfReader(source)
        plain = [(page.extract_text() or "").strip() for page in reader.pages]
        needs_ocr = [i + 1 for i, text in enumerate(plain) if len(text) < 40]
        language = item.get("language", "ru")
        cache = cache_dir / f"{pdf_hash[:20]}-{chosen_engine}-{language}-{scale}"
        cache.mkdir(parents=True, exist_ok=True)
        ocr_file = cache / "ocr.jsonl"
        ocr_by_page = {}
        if needs_ocr:
            if ocr_file.exists():
                cached = read_jsonl(ocr_file)
                ocr_by_page = {row["page"]: row for row in cached}
            if set(ocr_by_page) != set(needs_ocr):
                if not shutil.which(pdftoppm):
                    raise RuntimeError("Scanned PDFs require Poppler pdftoppm on PATH")
                # One render per document. Cache identity includes PDF hash, engine and scale.
                prefix = cache / "page"
                run_checked([pdftoppm, "-scale-to", str(scale), "-png", str(source.resolve()), str(prefix.resolve())])
                images = sorted(cache.glob("page-*.png"), key=lambda p: int(p.stem.split("-")[-1]))
                image_by_page = {int(p.stem.split("-")[-1]): p for p in images}
                if not set(needs_ocr).issubset(image_by_page):
                    raise ValueError("Rendered page inventory is incomplete")
                jobs = [{"page": i, "image": str(image_by_page[i].resolve())} for i in needs_ocr]
                if chosen_engine == "windows":
                    jobs_path = cache / "jobs.json"
                    write_json(jobs_path, jobs)
                    run_checked(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                 str(ROOT / "scripts" / "windows_ocr.ps1"), "-JobsPath", str(jobs_path.resolve()),
                                 "-OutputPath", str(ocr_file.resolve()), "-LanguageTag", language])
                    ocr_by_page = {row["page"]: row for row in read_jsonl(ocr_file)}
                elif chosen_engine == "tesseract":
                    if not shutil.which("tesseract"):
                        raise RuntimeError("Install Tesseract with rus+eng language data, or choose Windows OCR")
                    records = []
                    for job in jobs:
                        target = cache / f"text-{job['page']}"
                        run_checked(["tesseract", job["image"], str(target), "-l", "rus+eng", "--psm", "3"])
                        records.append({"page": job["page"], "text": target.with_suffix(".txt").read_text(encoding="utf-8"),
                                        "extraction_method": "tesseract", "confidence": None})
                    ocr_file.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
                    ocr_by_page = {row["page"]: row for row in records}
                else:
                    raise ValueError("Unsupported OCR engine")
            if set(ocr_by_page) != set(needs_ocr):
                raise ValueError(f"OCR page inventory mismatch: {source.name}")
        for i, text in enumerate(plain, 1):
            ocr = ocr_by_page.get(i)
            if ocr:
                text = ocr["text"].strip()
            if not text:
                raise ValueError(f"Empty page after extraction: {source.name}, page {i}")
            all_pages.append({"source_id": item["source_id"], "pdf_sha256": pdf_hash, "page": i,
                              "text": text, "text_sha256": sha256(text.encode("utf-8")),
                              "extraction_method": ocr["extraction_method"] if ocr else "pypdf",
                              "ocr_numeric_values_verified": False})
        print(f"Extracted {source.name}: {len(plain)} pages, OCR {len(needs_ocr)}", flush=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in all_pages), encoding="utf-8")
    temporary.replace(output)
    return {"documents": len(entries), "pages": len(all_pages), "llm_calls": 0, "api_cost_rub": 0}


def page_chunks(text: str, max_chars: int = 1800, overlap: int = 200):
    """Yield exact raw-text spans; a chunk never crosses a PDF page."""
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start + max_chars // 2, end)
            if boundary > start:
                end = boundary
        yield start, end, text[start:end]
        if end == len(text):
            break
        start = max(start + 1, end - overlap)


def build_index(manifest_path: Path, pages_path: Path, database: Path):
    entries = manifest_entries(manifest_path)
    by_id = {item["source_id"]: item for item in entries}
    pages = read_jsonl(pages_path)
    if {p["source_id"] for p in pages} != set(by_id):
        raise ValueError("Page data and manifest source inventories differ")
    seen = set()
    hashes = {}
    for page in pages:
        key = (page["source_id"], page["page"])
        if key in seen or page["page"] < 1:
            raise ValueError("Duplicate or invalid page number")
        seen.add(key)
        if sha256(page["text"].encode("utf-8")) != page["text_sha256"]:
            raise ValueError("Page text hash mismatch")
        old_hash = hashes.setdefault(page["source_id"], page["pdf_sha256"])
        if old_hash != page["pdf_sha256"]:
            raise ValueError("Mixed PDF versions within one source")
        expected_hash = by_id[page["source_id"]].get("sha256")
        if expected_hash and expected_hash != old_hash:
            raise ValueError("Page source hash differs from manifest")
    for source_id in by_id:
        numbers = sorted(p["page"] for p in pages if p["source_id"] == source_id)
        expected_count = by_id[source_id].get("page_count", len(numbers))
        if numbers != list(range(1, expected_count + 1)):
            raise ValueError("Missing pages in source")
    database.parent.mkdir(parents=True, exist_ok=True)
    temporary = database.with_suffix(database.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    connection = sqlite3.connect(temporary)
    chunk_count = 0
    try:
        connection.executescript("""
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE documents(source_id TEXT PRIMARY KEY, metadata TEXT NOT NULL);
            CREATE TABLE pages(source_id TEXT, page INTEGER, text TEXT, pdf_sha256 TEXT,
                               extraction_method TEXT, PRIMARY KEY(source_id,page));
            CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, page INTEGER NOT NULL,
                                char_start INTEGER, char_end INTEGER, text TEXT NOT NULL, searchable INTEGER NOT NULL);
            CREATE VIRTUAL TABLE chunks_fts USING fts5(chunk_id UNINDEXED, text, tokenize='unicode61');
        """)
        connection.execute("INSERT INTO metadata VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
        for item in entries:
            metadata = dict(item, sha256=hashes[item["source_id"]])
            connection.execute("INSERT INTO documents VALUES(?,?)", (item["source_id"], json.dumps(metadata, ensure_ascii=False)))
        for page in pages:
            source_id = page["source_id"]
            connection.execute("INSERT INTO pages VALUES(?,?,?,?,?)", (source_id, page["page"], page["text"], page["pdf_sha256"], page["extraction_method"]))
            searchable = int(page["page"] not in by_id[source_id].get("exclude_from_search_pages", []))
            for start, end, text in page_chunks(page["text"]):
                # Chart axes / numeric OCR tables cannot safely stand in for expert prose.
                narrative_words = re.findall(r"[^\W_\d]{4,}", text, re.UNICODE)
                chunk_searchable = int(searchable and len(narrative_words) >= 45)
                identity = f"{source_id}:{page['pdf_sha256']}:{page['page']}:{start}:{end}:{page['text_sha256']}"
                chunk_id = "ev_" + sha256(identity.encode("utf-8"))[:24]
                connection.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?)", (chunk_id, source_id, page["page"], start, end, text, chunk_searchable))
                connection.execute("INSERT INTO chunks_fts VALUES(?,?)", (chunk_id, text.lower().replace("ё", "е")))
                chunk_count += 1
        connection.commit()
    finally:
        connection.close()
    temporary.replace(database)
    return {"schema_version": SCHEMA_VERSION, "documents": len(entries), "pages": len(pages),
            "chunks": chunk_count, "search": "SQLite FTS5 / term coverage + BM25", "llm_calls": 0, "api_cost_rub": 0}


def query_terms(query: str):
    tokens = re.findall(r"[^\W_]+", query.lower().replace("ё", "е"), re.UNICODE)
    tokens = [x for x in tokens if len(x) >= 2 and x not in STOPWORDS]
    expanded = []
    for token in tokens[:32]:
        prefix = next((p for p in PREFIXES if token.startswith(p)), None)
        term = '"' + (prefix or token) + '"' + ("*" if prefix else "")
        if term not in expanded:
            expanded.append(term)
    return expanded


def open_database(database: Path):
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    version = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    if not version or int(version[0]) != SCHEMA_VERSION:
        connection.close()
        raise ValueError("Unsupported index schema")
    return connection


def retrieve(database: Path, query: str, as_of: date, limit: int = 5, max_age_days: int = 365,
             include_historical: bool = False, source_id: str | None = None, include_undated: bool = False,
             mode: str = 'lexical'):
    if not 1 <= limit <= 100 or max_age_days < 0:
        raise ValueError("Invalid result limit or max_age_days")
    if mode not in ('lexical', 'semantic', 'hybrid'):
        raise ValueError('Unknown retrieval mode')
    terms = query_terms(query)
    if not terms:
        return []
    connection = open_database(database)
    try:
        records = connection.execute("""
            SELECT c.*, bm25(chunks_fts) AS bm25_score, d.metadata, p.extraction_method
            FROM chunks_fts JOIN chunks c ON c.chunk_id=chunks_fts.chunk_id
            JOIN documents d ON d.source_id=c.source_id
            JOIN pages p ON p.source_id=c.source_id AND p.page=c.page
            WHERE chunks_fts MATCH ? AND c.searchable=1
            ORDER BY bm25_score, c.chunk_id
        """, (" OR ".join(terms),)).fetchall()
        def matched_terms(record):
            tokens = re.findall(r"[^\W_]+", record["text"].lower().replace("ё", "е"), re.UNICODE)
            matches = []
            for term in terms:
                value = term.strip('"*')
                if any(token.startswith(value) if term.endswith("*") else token == value for token in tokens):
                    matches.append(value)
            return matches
        records = sorted(records, key=lambda row: (-len(matched_terms(row)), row["bm25_score"], row["chunk_id"]))
        if mode != 'lexical':
            # Date/source eligibility applies before either ranking channel. The
            # dense channel scans all eligible vectors, not only lexical hits.
            def eligible(record):
                if source_id and source_id != record['source_id']:
                    return False
                metadata = json.loads(record['metadata'])
                published = metadata.get('publication_date')
                if not published:
                    return include_undated
                if metadata.get('date_review_status') != 'visual_verified':
                    return False
                age = (as_of-date.fromisoformat(published)).days
                return age >= 0 and (age <= max_age_days or include_historical)
            lexical_order = [record['chunk_id'] for record in records if eligible(record)]
            dense_records = connection.execute('SELECT c.*,NULL AS bm25_score,d.metadata,p.extraction_method '
                'FROM chunks c JOIN documents d ON d.source_id=c.source_id '
                'JOIN pages p ON p.source_id=c.source_id AND p.page=c.page WHERE c.searchable=1').fetchall()
            from alfa_app.semantic import rank_chunks
            records = rank_chunks(database, query, [dict(row) for row in dense_records if eligible(row)],
                                  lexical_order, mode)
        results = []
        seen_pages = set()
        for record in records:
            metadata = json.loads(record["metadata"])
            if source_id and source_id != record["source_id"]:
                continue
            published = metadata.get("publication_date")
            # Unknown dates and future documents cannot support a dated claim by default.
            if published:
                if metadata.get("date_review_status") != "visual_verified":
                    continue
                age = (as_of - date.fromisoformat(published)).days
                if age < 0 or (age > max_age_days and not include_historical):
                    continue
            else:
                if not include_undated:
                    continue
                age = None
            page_key = (record["source_id"], record["page"])
            if page_key in seen_pages:
                continue
            seen_pages.add(page_key)
            results.append({"evidence_id": record["chunk_id"], "source_id": record["source_id"],
                            "title": metadata.get("title") or metadata["file_name"],
                            "file_name": metadata["file_name"], "page": record["page"],
                            "publication_date": published, "age_days": age,
                            "report_period": metadata.get("report_period"),
                            "publication_date_unknown": published is None,
                            "historical_for_query": age is not None and age > max_age_days,
                            "source_url": metadata["source_url"],
                            "page_url": metadata["source_url"] + f"#page={record['page']}",
                            "pdf_sha256": metadata["sha256"], "text": record["text"],
                            "char_start": record["char_start"], "char_end": record["char_end"],
                            "bm25_score": record["bm25_score"],
                            "query_terms_matched": matched_terms(record),
                            "extraction_method": record["extraction_method"],
                            "warning": "OCR text: verify quotations/numbers against the original page; relevance score is not confidence."
                            + (" Publication date unknown: availability as of query date is not established." if published is None else "")})
            results[-1]['retrieval_method'] = mode
            if mode != 'lexical':
                results[-1].update({key: record[key] for key in
                    ('semantic_score', 'semantic_rank', 'lexical_rank', 'hybrid_score')})
            if len(results) == limit:
                break
        return results
    finally:
        connection.close()


def verify_quote(database: Path, source_id: str, page: int, quote: str):
    connection = open_database(database)
    try:
        row = connection.execute("SELECT * FROM pages WHERE source_id=? AND page=?", (source_id, page)).fetchone()
        present = bool(row and quote.strip() and quote in row["text"])
        return {"source_id": source_id, "page": page, "quote_present_in_extracted_text": present,
                "semantic_entailment_checked": False, "numeric_values_visually_verified": False,
                "warning": "An exact OCR quote match does not establish correctness or support for a conclusion."}
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    extract = commands.add_parser("extract")
    extract.add_argument("--pdf-dir", required=True, type=Path)
    extract.add_argument("--manifest", type=Path, default=ROOT / "data/research_manifest.json")
    extract.add_argument("--output", type=Path, default=ROOT / "data/research_pages.jsonl")
    extract.add_argument("--cache-dir", type=Path, default=ROOT / ".cache/ocr")
    extract.add_argument("--ocr-engine", choices=["auto", "windows", "tesseract"], default="auto")
    extract.add_argument("--scale", type=int, default=2600)
    extract.add_argument("--pdftoppm", default="pdftoppm")
    build = commands.add_parser("build")
    build.add_argument("--manifest", type=Path, default=ROOT / "data/research_manifest.json")
    build.add_argument("--pages", type=Path, default=ROOT / "data/research_pages.jsonl")
    build.add_argument("--database", type=Path, default=ROOT / "data/research.sqlite")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--database", type=Path, default=ROOT / "data/research.sqlite")
    search.add_argument("--as-of", type=date.fromisoformat, default=datetime.now(timezone(timedelta(hours=3))).date())
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--max-age-days", type=int, default=365)
    search.add_argument("--include-historical", action="store_true")
    search.add_argument("--include-undated", action="store_true")
    search.add_argument("--source-id")
    search.add_argument('--mode',choices=['hybrid','semantic','lexical'],default='hybrid')
    search.add_argument("--output", type=Path)
    verify = commands.add_parser("verify-quote")
    verify.add_argument("--database", type=Path, default=ROOT / "data/research.sqlite")
    verify.add_argument("--source-id", required=True)
    verify.add_argument("--page", type=int, required=True)
    verify.add_argument("--quote", required=True)
    args = parser.parse_args()
    if args.command == "extract":
        result = extract_pages(args.pdf_dir, args.manifest, args.output, args.cache_dir, args.ocr_engine, args.scale, args.pdftoppm)
    elif args.command == "build":
        result = build_index(args.manifest, args.pages, args.database)
    elif args.command == "search":
        result = {"query": args.query, "as_of": str(args.as_of), "results": retrieve(args.database, args.query, args.as_of,
                  args.limit, args.max_age_days, args.include_historical, args.source_id, args.include_undated,args.mode), "llm_calls": 0,
                  'retrieval_method':args.mode}
        if args.output:
            write_json(args.output, result)
    else:
        result = verify_quote(args.database, args.source_id, args.page, args.quote)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)

"""Verified CBR RSS sources, full-article extraction and content/URL deduplication."""
from datetime import datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, urljoin
import xml.etree.ElementTree as ET
from .config import MSK, now_msk
from .ranking import classify

FEEDS = {'cbr_news':'https://www.cbr.ru/rss/eventrss','cbr_press':'https://www.cbr.ru/rss/RssPress',
         'rosstat_news':'https://rosstat.gov.ru/folder/313/rss'}
ROSSTAT_CA=Path(__file__).resolve().parents[1]/'data/certificates/rosstat-ca.pem'
ROSSTAT_CA_SHA256='4ab21fc3f37793557c69279432f5def448acbd8aaf578e1112a40aa5773cbd89'
VOID = {'br','img','meta','link','input','hr','source','wbr','area','embed','param','track'}
EXTRACTION_VERSION='publisher-main-content-v2'
CHROME_CLASSES={'subscribe','subscription','newsletter','footer','site-footer','nav','menu','social','share',
                'breadcrumbs','cookie','comments','comment','toggle-card','big-banner','link-list','misprint',
                'modal','popup','feedback','accessibility'}
BLOCKS={'p','li','div','section','article','h2','h3','h4','h5','h6','tr','table','blockquote'}


def is_chrome(tag,values):
    classes=set((values.get('class') or '').split())
    return (tag in {'script','style','nav','footer','aside','form','button','select','noscript','svg','template'}
        or values.get('role') in {'navigation','contentinfo','dialog'} or values.get('aria-hidden')=='true'
        or any(token==name or token.startswith((name+'__',name+'--',name+'-')) for token in classes for name in CHROME_CLASSES))


def canonical_url(url):
    parts = urlsplit(url)
    if (len(url)>4096 or parts.scheme != 'https' or parts.hostname not in ['www.cbr.ru','cbr.ru','rosstat.gov.ru','www.rosstat.gov.ru']
            or parts.username or parts.password or parts.port not in [None,443]):
        raise ValueError('Адрес не входит в список разрешённых HTTPS-источников.')
    rosstat=parts.hostname.endswith('rosstat.gov.ru')
    if not (re.fullmatch(r'/folder/\d+/(?:rss|document/\d+)/?',parts.path) if rosstat else
            parts.path.lower().startswith(('/press/','/rss/'))):
        raise ValueError('Разрешены только публикации и RSS зарегистрированных издателей.')
    query = urlencode(sorted((k,v) for k,v in parse_qsl(parts.query,keep_blank_values=True) if not k.lower().startswith('utm_')))
    return urlunsplit(('https','rosstat.gov.ru' if rosstat else 'www.cbr.ru',parts.path,query,''))


def publisher(url):
    return 'Росстат' if urlsplit(url).hostname=='rosstat.gov.ru' else 'Банк России'


def native_tls_fetch(url,timeout):
    """Use Windows' verified TLS client when a managed HTTPS proxy needs it.

    No -k, automatic redirects, credentials or shell are used. CA trust stays in
    the user's operating system; personal certificates never enter the package.
    """
    executable=shutil.which('curl.exe') if os.name=='nt' else None
    if not executable:raise OSError('Native TLS transport is unavailable')
    marker=b'\nALFA_FETCH_META\n'
    command=[executable,'--silent','--show-error','--proto','=https','--max-time',str(timeout),
             '--max-filesize','2000000','--user-agent','AlfaNewsInsights/1.0 (public RSS reader)',
             '--write-out','\nALFA_FETCH_META\n%{http_code}\n%{redirect_url}',url]
    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,stdin=subprocess.DEVNULL)
    try:
        raw=process.stdout.read(2_004_200)
        if len(raw)>2_004_096:raise ValueError('Source exceeds the 2 MB limit')
        code=process.wait(timeout=timeout+2)
        if code:raise OSError('Native TLS source request failed')
        body,separator,metadata=raw.rpartition(marker)
        if not separator or len(body)>2_000_000:raise ValueError('Invalid native source response')
        fields=metadata.decode('utf-8').split('\n',1)
        return int(fields[0]),body,fields[1].strip() if len(fields)>1 else ''
    finally:
        if process.poll() is None:process.kill();process.wait()
        process.stdout.close();process.stderr.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def news_tls_context(url):
    """Publisher-specific public CA chain; never change system or LLM trust."""
    context=ssl.create_default_context()
    if publisher(canonical_url(url))=='Росстат':
        raw=ROSSTAT_CA.read_bytes()
        if sha256(raw).hexdigest()!=ROSSTAT_CA_SHA256:
            raise ValueError('Повреждена проверенная цепочка HTTPS Росстата.')
        context.load_verify_locations(cadata=raw.decode('ascii'))
    if os.getenv('ALFA_NEWS_CA_BUNDLE'):context.load_verify_locations(os.environ['ALFA_NEWS_CA_BUNDLE'])
    return context


def fetch(url, timeout=15):
    current = canonical_url(url)
    original_publisher=publisher(current)
    context=news_tls_context(current)
    opener = urllib.request.build_opener(NoRedirect,urllib.request.HTTPSHandler(context=context))
    for _ in range(4):
        request = urllib.request.Request(current,headers={'User-Agent':'AlfaNewsInsights/1.0 (AlfaFuture case 3)','Accept-Encoding':'identity'})
        try:
            with opener.open(request,timeout=timeout) as response:
                raw = response.read(2_000_001)
                if len(raw)>2_000_000:
                    raise ValueError('Source exceeds the 2 MB limit')
                return raw, response.headers.get_content_charset() or 'utf-8'
        except urllib.error.HTTPError as error:
            if error.code in [301,302,303,307,308] and error.headers.get('Location'):
                current = canonical_url(urljoin(current,error.headers['Location']))
                if publisher(current)!=original_publisher:raise ValueError('Cross-publisher redirect is forbidden')
                continue
            raise
        except urllib.error.URLError as error:
            if not isinstance(error.reason,ssl.SSLCertVerificationError) or os.name!='nt':raise
            status,raw,redirect=native_tls_fetch(current,timeout)
            if status in (301,302,303,307,308) and redirect:
                current=canonical_url(redirect)
                if publisher(current)!=original_publisher:raise ValueError('Cross-publisher redirect is forbidden')
                continue
            if status!=200:raise OSError('Source returned HTTP '+str(status))
            return raw,'utf-8'
    raise ValueError('Too many redirects')


class ArticleParser(HTMLParser):
    def __init__(self,content_classes=None):
        super().__init__(convert_charrefs=True)
        self.stack=[]
        self.content=[]
        self.title=[]
        self.date=[]
        self.metadata={}
        self.content_classes=content_classes or {'landing-text','lead-text'}

    def handle_starttag(self,tag,attrs):
        values=dict(attrs)
        classes=set((values.get('class') or '').split())
        if tag=='meta':
            self.metadata[values.get('property',values.get('name',''))] = values.get('content','')
        if tag not in VOID:
            chrome=is_chrome(tag,values)
            self.stack.append((tag,bool(classes & self.content_classes),tag=='h1','news-info-line_date' in classes,chrome))
        if (tag in BLOCKS or tag=='br') and self.in_content:
            self.content.append('\n')
        elif tag in {'td','th'} and self.in_content:self.content.append('\t')

    @property
    def in_content(self):
        return any(x[1] for x in self.stack) and not any(x[4] for x in self.stack)

    def handle_startendtag(self,tag,attrs):
        if tag in VOID:
            self.handle_starttag(tag,attrs)

    def handle_endtag(self,tag):
        if tag in BLOCKS and self.in_content:
            self.content.append('\n')
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0]==tag:
                del self.stack[i:]
                break

    def handle_data(self,data):
        if any(x[4] for x in self.stack):
            return
        if self.in_content:
            self.content.append(data)
        if any(x[2] for x in self.stack):
            self.title.append(data)
        if any(x[3] for x in self.stack):
            self.date.append(data)


def clean(text):
    return '\n'.join(re.sub(r'[\t \xa0]+',' ',line).strip() for line in text.splitlines() if line.strip()).strip()


class FeedTextParser(HTMLParser):
    """Read publisher-supplied RSS HTML as text; never execute or fetch its markup."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[]
        self.ignored=[]

    def handle_starttag(self,tag,attrs):
        if (self.ignored or is_chrome(tag,dict(attrs))) and tag not in VOID:
            self.ignored.append(tag)
        if (tag in BLOCKS or tag=='br') and not self.ignored:
            self.parts.append('\n')
        elif tag in {'th','td'} and not self.ignored:self.parts.append('\t')

    def handle_endtag(self,tag):
        if self.ignored and tag==self.ignored[-1]:
            self.ignored.pop()
        if tag in BLOCKS and not self.ignored:
            self.parts.append('\n')

    def handle_data(self,data):
        if not self.ignored:
            self.parts.append(data)


def feed_text(html):
    parser=FeedTextParser()
    parser.feed(html)
    return clean(''.join(parser.parts))


def excerpt_facts(text):
    facts=[]
    for sentence in re.split(r'(?<=[.!?])\s+(?=[А-ЯA-Z])',text)[:3]:
        quote=sentence[:700]
        start=text.find(quote)
        facts.append({'fact_id':'news_'+sha256(quote.encode()).hexdigest()[:16],'text':quote,
                      'char_start':start,'char_end':start+len(quote),'kind':'source_excerpt','semantic_fact_check':False})
    return facts


def event_from_feed(item,feed_id,feed_url,rss_hash,article_unavailable=False):
    """Use actual publisher text in the RSS when the separate article is unavailable."""
    html=item.findtext('{http://purl.org/rss/1.0/modules/content/}encoded') or item.findtext('description','')
    text=feed_text(html)
    title=feed_text(item.findtext('title',''))
    if not title or len(text)<120 or len(re.findall(r'[^\W\d_]+',text))<12:
        raise ValueError('RSS does not contain enough publisher text; headlines are not fabricated into articles')
    canonical=canonical_url(item.findtext('link',''))
    published=parsedate_to_datetime(item.findtext('pubDate',''))
    if published.tzinfo is None:
        raise ValueError('RSS publication timestamp must include timezone')
    return {'event_id':'news_'+sha256(canonical.encode()).hexdigest()[:20],'title':title,
            'published_at':published.astimezone(MSK).isoformat(),'event_date':None,
            'event_date_note':'Дата события отдельно не установлена; дата публикации взята из официального RSS.',
            'publisher':publisher(canonical),'source_url':canonical,'feed_id':feed_id,
            'text':text,'facts':excerpt_facts(text),'topics':classify(title,text),
            'content_sha256':sha256(text.encode()).hexdigest(),'rss_snapshot_sha256':rss_hash,
            'rss_url':feed_url,'retrieved_at':now_msk().isoformat(),
            'ingestion_method':'publisher_rss_text','content_scope':'publisher_rss',
            'full_article_available':False,'is_historical_example':False,
            'article_access':'unavailable' if article_unavailable else 'not_requested',
            'source_warning':'Текст предоставлен издателем в официальном RSS. '+
                ('Отдельная веб-страница недоступна; её полный текст не проверен.' if article_unavailable else
                 'Содержание отдельной веб-страницы не загружалось.')}


def parse_article(raw,charset='utf-8',content_classes=None):
    parser=ArticleParser(content_classes)
    parser.feed(raw.decode(charset,errors='replace'))
    title=clean(''.join(parser.title)) or parser.metadata.get('og:title','')
    text=clean(''.join(parser.content))
    # Press releases use the same landing-text block. Do not fall back to menus/footer text.
    if not title:
        title=parser.metadata.get('title','')
    if len(text)<80:
        raise ValueError('Не найден достаточный текст статьи; меню не используется как новость.')
    return {'title':title,'text':text,'date_text':clean(''.join(parser.date)),'metadata':parser.metadata}


def event_from_article(url,raw,published_at,title='',charset='utf-8',feed_id='manual'):
    canonical=canonical_url(url)
    article=parse_article(raw,charset,{'content'} if publisher(canonical)=='Росстат' else None)
    if not published_at:
        candidate=article['metadata'].get('article:published_time') or article['metadata'].get('datePublished')
        if candidate:
            published_at=datetime.fromisoformat(candidate.replace('Z','+00:00')).isoformat()
        else:
            match=re.search(r'(\d{2})\.(\d{2})\.(\d{4})',article['date_text'])
            if not match:
                raise ValueError('Дата публикации не найдена; она не заменяется датой загрузки.')
            day,month,year=map(int,match.groups())
            published_at=datetime(year,month,day,tzinfo=MSK).isoformat()
    dt=datetime.fromisoformat(published_at.replace('Z','+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Publication timestamp must include timezone')
    title=article['title'] or title
    if not title:
        raise ValueError('Article title missing')
    text=article['text']
    return {'event_id':'news_'+sha256(canonical.encode()).hexdigest()[:20],'title':title,
            'published_at':dt.astimezone(MSK).isoformat(),'event_date':None,
            'event_date_note':'Дата события отдельно не установлена; дата публикации не подменяет её.',
            'publisher':publisher(canonical),'source_url':canonical,'feed_id':feed_id,'text':text,
            'facts':excerpt_facts(text),'topics':classify(title,text),'content_sha256':sha256(text.encode()).hexdigest(),
            'raw_sha256':sha256(raw).hexdigest(),'retrieved_at':now_msk().isoformat(),
            'ingestion_method':'verified_rss_and_full_article','is_historical_example':False,
            'article_date_text':article['date_text'],'content_scope':'article','full_article_available':True,
            'extraction_version':EXTRACTION_VERSION}


def refresh(store,raw_dir,limit=5,feeds=None,fetcher=fetch,progress=None,prefer_feed_text=True):
    if not 1<=limit<=15:
        raise ValueError('Feed limit must be between 1 and 15')
    raw_dir=Path(raw_dir)
    raw_dir.mkdir(parents=True,exist_ok=True)
    report={'inserted':0,'updated':0,'duplicate':0,'rss_text_imported':0,'kept_full_text':0,
            'errors':[],'warnings':[],'feeds':[],'verified_on':now_msk().isoformat()}
    for feed_id,url in (feeds or FEEDS).items():
        if progress:progress({'step':'refresh','message':'Загружаем официальный RSS: '+feed_id})
        try:
            raw,_=fetcher(url)
            tree=ET.fromstring(raw)
            items=tree.findall('.//item')
            if not items:
                raise ValueError('RSS contains no items')
            (raw_dir/(sha256(raw).hexdigest()+'.xml')).write_bytes(raw)
            report['feeds'].append({'feed_id':feed_id,'source_url':url,'items_available':len(items),'raw_sha256':sha256(raw).hexdigest()})
        except (OSError,ValueError,ET.ParseError) as error:
            report['errors'].append({'source':feed_id,'error':type(error).__name__,
                                    'http_status':getattr(error,'code',None)})
            continue
        for item in items[:limit]:
            try:
                link=canonical_url(item.findtext('link',''))
                pub=parsedate_to_datetime(item.findtext('pubDate',''))
                if pub.tzinfo is None:raise ValueError('RSS publication timestamp must include timezone')
                pub=pub.astimezone(MSK)
                if pub>now_msk():
                    continue
                if progress:progress({'step':'refresh','message':'Читаем публикацию: '+item.findtext('title','')[:120]})
                existing=store.event_for_url(link)
                full_existing=existing and (existing.get('full_article_available') or existing.get('ingestion_method')=='verified_rss_and_full_article')
                rss_event=None
                if prefer_feed_text and not full_existing:
                    try:rss_event=event_from_feed(item,feed_id,url,sha256(raw).hexdigest())
                    except ValueError:pass
                if rss_event:
                    _,status=store.put_event(rss_event)
                    report[status]+=1
                    report['rss_text_imported']+=1
                    continue
                try:
                    source,charset=fetcher(link)
                    event=event_from_article(link,source,pub.isoformat(),item.findtext('title',''),charset,feed_id)
                    (raw_dir/(event['raw_sha256']+'.html')).write_bytes(source)
                except (OSError,ValueError,TypeError) as error:
                    event=event_from_feed(item,feed_id,url,sha256(raw).hexdigest(),article_unavailable=True)
                    report['warnings'].append({'source':link,'http_status':getattr(error,'code',None),
                        'reason':type(error).__name__,'action':'publisher_rss_text'})
                    existing=store.event_for_url(event['source_url'])
                    if existing and (existing.get('full_article_available') or existing.get('ingestion_method')=='verified_rss_and_full_article'):
                        report['kept_full_text']+=1
                        report['duplicate']+=1
                        continue
                    report['rss_text_imported']+=1
                _,status=store.put_event(event)
                report[status]+=1
            except (OSError,ValueError,TypeError) as error:
                report['errors'].append({'source':item.findtext('link',''),'error':type(error).__name__,
                                        'http_status':getattr(error,'code',None)})
    return report

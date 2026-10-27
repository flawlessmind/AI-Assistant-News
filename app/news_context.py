"""Select literal bounded spans from the whole article, without invented quotes."""
import re

POLICY_VERSION='whole-article-spans-v1'
SEPARATOR='\n[…]\n'
LIMITATIONS=re.compile(r'однако|только|исключ|без учет|без учёт|предварител|уточнен|уточнён|огранич|'
    r'не (?:означ|распростран|учитыва|явля|подтвержд)|сезон|сопоставим|за (?:период|месяц|квартал)|'
    r'сценар|при услов|в случае',re.I)
PERIODS=re.compile(r'\b(?:19|20)\d{2}\b|\d{1,2}[./]\d{1,2}|январ|феврал|март|апрел|ма[йяе]|июн|июл|август|сентябр|октябр|ноябр|декабр|квартал|полугоди',re.I)


def terms(text):
    return {word[:5] for word in re.findall(r'[^\W\d_]{3,}',text.casefold())}


def article_spans(text,span_limit=700):
    spans=[]
    # Keep exact offsets; never concatenate remote paragraphs into one quote.
    for match in re.finditer(r'[^\n]+',text):
        start,end=match.span()
        while start<end:
            while start<end and text[start].isspace():start+=1
            if start==end:break
            stop=min(end,start+span_limit)
            if stop<end:
                boundaries=list(re.finditer(r'(?<=[.!?])\s+',text[start:stop]))
                split=start+boundaries[-1].start() if boundaries else text.rfind(' ',start,stop)
                if split>start:stop=split
            while stop>start and text[stop-1].isspace():stop-=1
            if stop<=start:stop=min(end,start+span_limit)
            spans.append({'char_start':start,'char_end':stop,'text':text[start:stop]})
            start=stop
    return spans


def select_news_context(text,title='',question='',limit=3500):
    if type(limit) is not int or limit<200:raise ValueError('Invalid article context limit')
    if len(text)<=limit:
        return {'text':text,'fragments':[{'char_start':0,'char_end':len(text),'text':text}],
                'text_truncated':False,'context_incomplete':False,'omitted_important_fragments':0,
                'original_chars':len(text),'selection_policy':POLICY_VERSION,'context_warning':None}
    spans=article_spans(text,min(700,limit//2))
    query,title_terms=terms(question),terms(title)
    for index,span in enumerate(spans):
        words=terms(span['text'])
        span['important']=bool(LIMITATIONS.search(span['text']) or PERIODS.search(span['text']))
        span['score']=(12*len(words&query)+3*len(words&title_terms)+6*bool(LIMITATIONS.search(span['text']))
                       +2*bool(PERIODS.search(span['text']))+3*(index==0)+3*(index==len(spans)-1))
    chosen=[];used=0
    for span in sorted(spans,key=lambda part:(-part['score'],part['char_start'])):
        cost=len(span['text'])+(len(SEPARATOR) if chosen else 0)
        if used+cost<=limit:chosen.append(span);used+=cost
    chosen.sort(key=lambda span:span['char_start'])
    included={span['char_start'] for span in chosen}
    omitted=sum(span['important'] and span['char_start'] not in included for span in spans)
    warning=('Передана выборка дословных фрагментов из всей статьи, а не полный текст. '
             'Разделитель […] не является цитатой. Не делай вывод об отсутствии сведений '
             'в пропущенных частях; сохраняй исходные периоды и ограничения.')
    if omitted:warning+=f' Не поместилось фрагментов с датами или оговорками: {omitted}; полнота существенного контекста не подтверждена.'
    return {'text':SEPARATOR.join(span['text'] for span in chosen),
            'fragments':[{key:span[key] for key in ('char_start','char_end','text')} for span in chosen],
            'text_truncated':True,'context_incomplete':True,'omitted_important_fragments':omitted,
            'original_chars':len(text),'selection_policy':POLICY_VERSION,'context_warning':warning}


def selected_quotes(context,extractor,title='',question=''):
    candidates=list(dict.fromkeys(quote for span in context['fragments'] for quote in extractor(span['text'])))
    query,title_terms=terms(question),terms(title)
    return sorted(candidates,key=lambda quote:-(12*len(terms(quote)&query)+2*len(terms(quote)&title_terms)
                  +4*bool(LIMITATIONS.search(quote))))[:3]

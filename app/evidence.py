"""Full-page semantic context plus conservative guards for known scope mismatches."""
import hashlib
import json
import re
from alfa_research import open_database

POLICY_VERSION='2026-10-21-scope-v1'


def forecast_claim(text):
    # A negated noun ("это не прогноз") does not turn an observation into a forecast.
    return bool(re.search(r'прогноз',re.sub(r'\bне\s+(?:является\s+)?прогноз(?:ом)?\b','',text,flags=re.I),re.I))


def attach_page_context(database,evidence):
    connection=open_database(database)
    try:
        result=[]
        for item in evidence:
            row=connection.execute('SELECT text FROM pages WHERE source_id=? AND page=?',(item['source_id'],item['page'])).fetchone()
            if not row:
                raise ValueError('Страница банковского доказательства отсутствует.')
            text=row['text']
            result.append(dict(item,page_context=text,page_context_sha256=hashlib.sha256(text.encode()).hexdigest()))
        return result
    finally:
        connection.close()


def model_evidence(evidence,quotes):
    """Send whole pages once, including qualifiers beyond the retrieved fragment."""
    result=[];seen=set();used=0
    for source in evidence:
        key=(source['source_id'],source['page'])
        if key in seen:
            continue
        item={'evidence_id':source['evidence_id'],'publication_date':source['publication_date'],
              'report_period':source.get('report_period'),'title':source['title'],'page':source['page'],
              'text':source['page_context'],'page_context_sha256':source['page_context_sha256'],
              'quote_options':quotes(source['text']),
              'quote_rule':'Цитировать только quote_options данного фрагмента; полный текст страницы дан для проверки смысла.'}
        item['usage_constraints']=source_constraints(source)
        size=len(json.dumps(item,ensure_ascii=False).encode())
        # Leave room for instructions, question, draft and review under the HTTP client's bound.
        if used+size>43000:
            continue
        result.append(item);seen.add(key);used+=size
    return result


def source_constraints(source):
    """Extract explicit scope cues from the given page, without new bank claims."""
    text=source.get('page_context',source.get('text',''))
    result=[]
    if re.search(r'автомобил',text,re.I) and re.search(r'непродовольствен',text,re.I):
        result.append('На странице есть пример автомобилей в непродовольственной рознице. Если используешь этот эпизод, сохрани автомобили в позиции банка; в scope_note отдели его от отрасли клиента.')
        observed=re.search(r'Оборот\s+розничной\s+торговли\s+в\s+(\w+)\s+вырос',text,re.I)
        if observed:result.append('Период этого наблюдения розницы — '+observed[1]+'. Назови его словами в bank_positions.text; дата публикации его не заменяет.')
        if re.search(r'ограничени[^.]{0,100}предложени',text,re.I|re.S):
            result.append('Страница содержит ограничение со стороны предложения. Не заменяй его установленным падением спроса; сохраняй различие наблюдения и сценария.')
    return result


def explicit_unknown_impact(text):
    return bool(re.search(r'не установ|не подтвержд|требует.*провер|нужны.*данн|влияни[^.;]{0,60}неизвестн|(?:данн|подтвержд)[^.;]{0,80}(?:собственн|клиент|компани|одежд)[^.;]{0,50}\bнет\b',text,re.I))


def unestablished_intragroup_payment(text):
    """A same-ID transfer or shared GROUP_ID cannot establish a group payment.

    Unknowns, explicit denials and requests to verify are allowed. A negation of
    a different predicate ("not an external receipt") does not establish origin.
    """
    if not isinstance(text,str):return False
    phrase=r'внутригруппов\w*\s+(?:денежн\w*\s+)?(?:перевод|плат[её]ж|поступлен)\w*|(?:перевод|плат[её]ж)\w*\s+внутри\s+группы'
    for sentence in re.split(r'[.!?;\n]+',text):
        for match in re.finditer(phrase,sentence,re.I):
            prefix=sentence[:match.start()]
            suffix=sentence[match.end():]
            if re.search(r'не (?:означа|доказыва|устанавлива|подтвержд)|нельзя|неизвест|не установ|не подтвержд|нет данных|не следует|есть ли|проверить|уточнить|если|при подтверждени',prefix,re.I):continue
            if re.match(r'\s*(?:не установлен|не подтвержд[её]н|не доказан|неизвестен|не следует)',suffix,re.I):continue
            return True
    return False


def semantic_issues(draft,profile,evidence):
    issues=[]
    clothing=profile['company']['industry_okved_code'].startswith('47.71')
    sources={item['evidence_id']:item for item in evidence}
    groups=['news_facts','bank_positions','interpretations','risks','opportunities','actions','points']
    all_items=[(group,item) for group in groups for item in (draft[group] if isinstance(draft.get(group),list) else []) if isinstance(item,dict)]
    scope_note=draft.get('scope_note','')
    claim_texts=[draft.get(key,'') for key in ('title','summary','scope_note')]+[item.get('text','') for _,item in all_items]
    if isinstance(draft.get('uncertainties'),list):claim_texts+=draft['uncertainties']
    if any(unestablished_intragroup_payment(text) for text in claim_texts):
        issues.append('intragroup_payment_not_established')
    for group,item in all_items:
        text=item.get('text','')
        if not isinstance(text,str):continue
        ids=item.get('evidence_ids',[])
        related=[sources[i] for i in (ids if isinstance(ids,list) else []) if isinstance(i,str) and i in sources]
        auto_context=any(re.search(r'автомобил',s.get('page_context',s['text']),re.I) and
                         re.search(r'непродовольствен',s.get('page_context',s['text']),re.I) for s in related)
        demand=re.search(r'замедл|охлажд|тормож|сниз|сниж|ослаб',text,re.I)
        if group=='bank_positions' or group=='points' and item.get('kind')=='source_fact' and related:
            if auto_context and demand and re.search(r'розниц|потреблен|спрос|непродовольствен',text,re.I) and not re.search(r'автомобил',text,re.I):
                issues.append('bank_segment_qualifier_missing')
            if auto_context and demand and re.search(r'розниц|потреблен|спрос|непродовольствен',text,re.I) and not (forecast_claim(text) or re.search(r'ожида|сценар',text,re.I)):
                for source in related:
                    observed=re.search(r'Оборот\s+розничной\s+торговли\s+в\s+(\w+)\s+вырос',source.get('page_context',source['text']),re.I)
                    if observed and observed[1].lower()[:3] not in text.lower():
                        issues.append('bank_observation_period_missing_or_changed')
            if clothing and auto_context and re.search(r'одежд',text,re.I) and not re.search(r'не (?:установ|подтвержд)|нет данных|не (?:измеря|измерител|индикатор|показател|описыва)[^.;]{0,100}одежд',text,re.I):
                issues.append('bank_does_not_establish_clothing_demand')
        elif group in ['interpretations','risks','opportunities','actions','points'] and clothing and auto_context and demand:
            if not (isinstance(scope_note,str) and re.search(r'автомобил',scope_note,re.I) and re.search(r'одежд',scope_note,re.I)
                    and re.search(r'не установ|не подтвержд|подтверждени[ея].{0,200}нет|данн|требует.*провер',scope_note,re.I|re.S)):
                issues.append('cross_segment_scope_not_explained')
            if group in ['interpretations','points'] and re.search(r'выручк|продаж|спрос',text,re.I) and not (explicit_unknown_impact(text) or re.search(r'сценари|гипотез|если.*(?:данн|продажи одежды|продажи клиента).*подтверд',text,re.I)):
                issues.append('clothing_summary_overstates_macro_evidence')
        if group not in ['news_facts','bank_positions'] and re.search(r'обслуживан.*(?:кредит|обязательств|финансирован)|действующ.*кредитн.*договор|кредитн.*обязательств',text,re.I):
            if not re.search(r'есть ли|при наличии[^.;]*(?:кредит|долг|договор|финансирован)|не подтвержд|не установ|если[^.;]*(?:действующ|подтвержден)[^.;]*(?:кредит|долг|договор|финансирован)|если[^.;]*(?:кредит|долг|договор|финансирован)[^.;]*(?:действ|подтвержд)',text,re.I):
                issues.append('active_debt_not_established')
    title=draft.get('title','')
    # A weaker currency is not a claim of weaker clothing demand. Preserve the
    # guard for actual demand/sales statements rather than matching an FX noun.
    demand_title=re.sub(r'(?:ослабл\w*|сниж\w*|замедл\w*)\s+(?:курса\s+)?(?:рубл\w*|доллар\w*|евро\w*)','',title,flags=re.I) if isinstance(title,str) else ''
    if clothing and isinstance(title,str) and re.search(r'одежд',title,re.I) and re.search(r'охлажд|замедл|сниж|ослаб',demand_title,re.I):
        if not re.search(r'провер|сценари|гипотез|не установ|вопрос|подтвержд|\bли\b[^?]*\?',title,re.I):
            issues.append('title_overstates_clothing_evidence')
    summary=draft.get('summary','')
    if clothing and isinstance(summary,str) and re.search(r'одежд',summary,re.I) and re.search(r'охлажд|замедл|сниж|ослаб',summary,re.I):
        if not (explicit_unknown_impact(summary) or re.search(r'сценари|гипотез',summary,re.I)):
            issues.append('clothing_summary_overstates_macro_evidence')
    return sorted(set(issues))

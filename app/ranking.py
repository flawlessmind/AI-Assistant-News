"""Explainable news ranking before generation; product flags never imply amounts."""
from datetime import date, datetime
import re

TOPICS = {
    'key_rate':('ключев','денежно-кредит','решение по ставке'),
    'inflation':('инфляц','потребительских цен','цены выросли','цены на топлив'),
    'consumer_demand':('розничной торгов','розничная торгов','розничных продаж','розничные продажи',
                       'оборот рознич','потребительский спрос','потребительского спроса',
                       'потребительская активность','потребительской активности','потребления','потреблени'),
    'construction':('строитель','ипотек','недвижим','жилищн'),
    'labor':('зарплат','рынок труда','занятост','безработиц'),
    'credit':('кредитован','кредитного портфеля','стоимость кредит','кредит экономике','займов бизнесу'),
    'currency':('курс рубля','валютн','валютного'),
    'budget':('федерального бюджета','бюджетных расход'),
    'transport_costs':('топлив','бензин','дизел','грузоперевоз','логистик','транспортных тариф'),
    'industry':('промышленн','производств','металлург','обрабатывающ'),
    'reporting':('отчетную нагрузку','отчётную нагрузку','статистической отчетности','статистической отчётности'),
    'economic_activity':('ввп','бизнес-климат','деловая активность','деловой активности','финансовых потоков'),
    'digital_payments':('цифровым рубл','цифрового рубл','цифровыми рубл','цифровой рубл'),
}
LABELS = {'key_rate':'Ставка и финансирование','inflation':'Инфляция','consumer_demand':'Потребительский спрос',
          'construction':'Строительство','labor':'Рынок труда','credit':'Кредитование','currency':'Валюта','budget':'Бюджет',
          'transport_costs':'Транспорт и топливо','industry':'Производство','reporting':'Отчётность',
          'economic_activity':'Деловая активность','digital_payments':'Платежи'}


def classify(title, text):
    combined = (title+' '+text[:5000]).lower().replace('ё','е')
    result=[topic for topic,roots in TOPICS.items() if any(root in combined for root in roots)]
    if 'key_rate' in result and not ('ключев' in combined and 'ставк' in combined):
        result.remove('key_rate')
    # A reference rate inside a household mortgage formula is not a new
    # monetary-policy decision applicable to every corporate borrower.
    if 'key_rate' in result and 'ипотек' in title.lower() and 'ключев' not in title.lower():
        result.remove('key_rate')
    if 'reporting' in result:
        result=[topic for topic in result if topic not in ('industry','labor')]
    if 'economic_activity' in result and not any(root in (title+' '+text[:500]).lower() for root in TOPICS['economic_activity']):
        result.remove('economic_activity')
    if 'digital_payments' in result and not any(root in title.lower() for root in ('инфляц','ставк','экономик')):
        result=['digital_payments']
    return result


def event_kind(title):
    text=title.lower()
    if re.search(r'приглаша|круглый стол|научн.*конференц|чемпионат|студент|диктант|конкурс|награжден|награждён|онлайн-заняти|финансовой грамотност|финтрека',text):
        return 'organizational_or_educational'
    if 'состоится пресс-конференция' in text or 'состоялось заседание национального' in text:
        return 'calendar_or_protocol'
    if (re.search(r'новый выпуск журнала|статистическ.*служб.*сотруднич|статистик.*сотруднич',text)
            or 'сотрудничество' in text and 'росстат' in text):
        return 'organizational_or_educational'
    return 'substantive_candidate'


def household_financial_material(event):
    """Do not turn retail financial products into corporate operating signals."""
    title=event['title'].lower()
    lead=(title+' '+event.get('text','')[:650]).lower()
    return (any(term in lead for term in ('страхования жизни','страхование жизни','исж 2.0','нсж'))
            or 'мфо' in title and 'потребительск' in lead and 'займов бизнесу' not in title)


def rank_event(profile, event, as_of, role='cfo'):
    if role not in ['cfo','ceo']:
        raise ValueError('Unknown role')
    published = date.fromisoformat(event['published_at'][:10])
    age = (as_of-published).days
    if age < 0:
        return None
    company = profile['company']
    okved = company.get('industry_okved_code','')
    products = company.get('products',{})
    event=dict(event,topics=classify(event['title'],event.get('text','')))
    topics = set(event['topics'])
    household_mortgage='ипотек' in event['title'].lower() and 'ключев' not in event['title'].lower()
    reasons = []
    score = 0

    def add(points, field, value, text):
        nonlocal score
        score += points
        reasons.append({'points':points,'company_field':field,'company_value':value,'text':text})

    if topics & {'economic_activity','key_rate','credit'} and not household_mortgage:
        add(2,'company.industry_okved_code',okved,'Общеэкономический контекст для планирования: индивидуальный спрос, долг и денежные потоки компании из этой статистики не следуют.')

    if okved.startswith('47'):
        if 'digital_payments' in topics:
            add(3,'company.industry_okved_code',okved,'Для розницы проверить готовность приёма платежей и применимость условий; подключение компании к платформе не установлено.')
        if topics & {'inflation','consumer_demand'}:
            add(5,'company.industry_okved_code',okved,'Розничная торговля: событие связано с потребительскими ценами или спросом. Влияние на продажи клиента ещё не установлено.')
        if 'key_rate' in topics:
            add(1,'company.industry_okved_code',okved,'Для розницы ставка может быть связана с покупательским спросом; индивидуальную связь нужно проверить.')
    if okved.startswith(('41','42','43')):
        if 'construction' in topics:
            add(6,'company.industry_okved_code',okved,'Строительная деятельность совпадает с темой события.')
        if topics & {'key_rate','inflation','budget'}:
            add(3,'company.industry_okved_code',okved,'Для строительства событие может влиять на условия спроса, закупок и финансирования проектов. Конкретный эффект требует данных о проектах.')
    division=okved.split('.')[0]
    if division.isdigit() and 10<=int(division)<=33:
        if 'industry' in topics:
            add(5,'company.industry_okved_code',okved,'Производственная деятельность: событие относится к промышленности. Применимость к продукции компании требует проверки.')
        if topics & {'inflation','transport_costs','key_rate'} and not household_mortgage:
            add(3,'company.industry_okved_code',okved,'Производству важны цены ресурсов и условия финансирования. Структура затрат и условия договоров компании не установлены.')
    if okved.startswith(('49','50','51','52','53')) and topics & {'transport_costs','inflation'}:
        add(5,'company.industry_okved_code',okved,'Транспорт и складирование: проверить связь события с тарифами, загрузкой и расходами. Состав затрат компании неизвестен.')
    if okved.startswith('46') and topics & {'consumer_demand','inflation','key_rate','transport_costs'} and not household_mortgage:
        add(4,'company.industry_okved_code',okved,'Оптовая торговля: проверить условия расчётов, оборот запасов и спрос по товарным группам компании.')
    if 'reporting' in topics:
        add(2,'company.industry_okved_code',okved,'Событие об отчётности бизнеса: применимость зависит от форм и обязанностей конкретной компании.')
    if products.get('credit') is True and topics & {'key_rate','credit'} and not household_mortgage:
        add(4,'company.products.credit',True,'В базе отмечен кредитный продукт. Уточнить наличие действующего долга и, если он есть, условия; сумма долга неизвестна.')
    elif products.get('credit') is True and 'inflation' in topics:
        add(1,'company.products.credit',True,'В базе отмечен кредитный продукт: изменение цен — повод проверить потребность в оборотном финансировании. Текущий долг и рост расходов этим не установлены.')
    if products.get('leasing') is True and (topics & {'key_rate','credit'} and not household_mortgage
            or 'construction' in topics and okved.startswith(('41','42','43'))):
        add(1,'company.products.leasing',True,'Отмечен лизинг: возможная связь с условиями договоров требует проверки; автоматический пересмотр платежей не предполагается.')
    if company.get('employee_band_fns') and 'labor' in topics:
        add(2,'company.employee_band_fns',company['employee_band_fns'],'Есть сведения о градации работников; событие о рынке труда может быть значимо для кадровых расходов.')
    if score and role == 'cfo' and topics & {'key_rate','credit','inflation','economic_activity'}:
        add(1,'user.selected_role','cfo','Выбран ракурс финансового директора: приоритет условиям финансирования и денежным потокам.')
    if score and role == 'ceo' and topics & {'consumer_demand','construction','labor','economic_activity','key_rate','credit'}:
        add(1,'user.selected_role','ceo','Выбран ракурс руководителя: приоритет спросу, операционной деятельности и ресурсам.')
    if score and role=='cfo' and topics & {'transport_costs','industry','reporting','digital_payments'} and not any(reason['company_field']=='user.selected_role' for reason in reasons):
        add(1,'user.selected_role','cfo','Финансовый ракурс: проверить затраты, сроки расчётов и требования к отчётности.')
    if score and role=='ceo' and topics & {'transport_costs','industry','reporting','digital_payments'} and not any(reason['company_field']=='user.selected_role' for reason in reasons):
        add(1,'user.selected_role','ceo','Управленческий ракурс: проверить загрузку, портфель заказов и операционные решения.')
    payments=profile.get('payments',{})
    counterparties=payments.get('outgoing_external_counterparty_count',0)
    if score and counterparties and topics & {'inflation','key_rate','credit','construction'}:
        add(0,'payments.outgoing_external_counterparty_count',counterparties,
            f'В предоставленном наборе у компании {counterparties} контрагентов по внешним исходящим платежам. '
            'Состав закупок и условия расчётов нужно уточнить; самопереводы исключены, период и валюта платежей не установлены.')
    network=profile.get('network',{})
    for direction,label in [('outgoing','исходящих'),('incoming','входящих')]:
        part=network.get(direction,{})
        sectors=part.get('industry_divisions',[])
        if score and sectors:
            names=', '.join(f"ОКВЭД {row['okved_division']}: {row['company_count']}" for row in sectors[:3])
            add(0,f'network.{direction}.industry_divisions',sectors,
                f'В {label} платёжных связях известна отраслевая структура контрагентов ({names}). '
                'Это число компаний, не доли расходов или продаж; назначение переводов не установлено.')
    group=network.get('group',{})
    if score and group.get('unique_company_count',0)>1:
        add(0,'network.group.unique_company_count',group['unique_company_count'],
            f"Один GROUP_ID объединяет {group['unique_company_count']} компаний в наборе. "
            'Для группового решения нужно уточнить периметр; контроль и консолидация не подтверждены.')
    freshness = round(max(0,1-age/60),2)
    kind=event_kind(event['title'])
    if kind!='substantive_candidate' or household_financial_material(event):
        score=0
        reasons=[]
    return {'event':event,'score':round(score+freshness,2),'business_score':score,'freshness_component':freshness,
            'age_days':age,'relevance_level':'high' if score >= 6 else 'medium' if score >= 3 else 'low',
            'event_kind':kind,
            'reasons':reasons,'role':role,'score_is_probability':False,
            'limits':['Нулевой кредитный маркер не доказывает отсутствие заимствований в других банках.',
                      'Регион регистрации и ФТС-флаг не превращены в географию операций или валютную экспозицию.']}


def ranked_feed(profile, events, as_of, role='cfo'):
    rows = [rank_event(profile,event,as_of,role) for event in events]
    return sorted([row for row in rows if row],key=lambda row:(row['score'],row['event']['published_at']),reverse=True)

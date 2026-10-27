"""Source adapters and scope boundaries; fixtures here are explicitly synthetic."""
from datetime import date
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import ssl
from alfa_app.news import canonical_url,event_from_article
from alfa_app.news import news_tls_context
from alfa_app.ranking import rank_event
ROOT=Path(__file__).resolve().parents[1]


class CurrentSourceTests(unittest.TestCase):
    def test_publisher_chain_preserves_hostname_and_required_certificate_verification(self):
        context=news_tls_context('https://rosstat.gov.ru/folder/313/rss')
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)

    def test_damaged_publisher_chain_blocks_rosstat_but_does_not_change_cbr_trust(self):
        with patch('alfa_app.news.ROSSTAT_CA_SHA256','invalid'):
            with self.assertRaises(ValueError):news_tls_context('https://rosstat.gov.ru/folder/313/rss')
            self.assertTrue(news_tls_context('https://www.cbr.ru/rss/eventrss').check_hostname)

    def test_rosstat_extracts_only_article_and_retains_real_publisher(self):
        text='Тестовый план по статистической отчётности. Это синтетический текст для проверки извлечения статьи, а не опубликованная новость.'
        raw=('<html><head><meta property="og:title" content="Тестовый план"></head><body><nav>Меню не новость</nav><h1>Тестовый план</h1><div class="content"><p>'+text+'</p></div><footer>Подвал не новость</footer></body></html>').encode()
        event=event_from_article('https://rosstat.gov.ru/folder/313/document/123',raw,'2026-10-20T12:00:00+03:00')
        self.assertEqual(event['publisher'],'Росстат')
        self.assertIn(text,event['text'])
        self.assertNotIn('Меню не новость',event['text'])
        self.assertNotIn('Подвал не новость',event['text'])

    def test_source_allowlist_rejects_aliases_credentials_http_and_non_article_paths(self):
        for url in ['http://rosstat.gov.ru/folder/313/rss','https://rosstat.gov.ru.evil.test/folder/313/rss',
                    'https://user:password@rosstat.gov.ru/folder/313/rss','https://rosstat.gov.ru/private',
                    'https://rosstat.gov.ru:444/folder/313/rss','https://127.0.0.1/folder/313/rss']:
            with self.subTest(url=url),self.assertRaises(ValueError):canonical_url(url)
        self.assertEqual(canonical_url('https://www.rosstat.gov.ru/folder/313/rss#fragment'),'https://rosstat.gov.ru/folder/313/rss')

    def test_financial_literacy_and_household_insurance_never_borrow_corporate_rate_relevance(self):
        profile=json.loads((ROOT/'data/company_U_1595.json').read_text('utf-8'))
        for title,text in [('Онлайн-занятия по финансовой грамотности','Инфляция, ключевая ставка и цены.'),
                           ('Порядок выплаты инвестдохода','Новый продукт страхования жизни. Инфляция и ключевая ставка.')]:
            event={'title':title,'text':text,'published_at':'2026-10-20T12:00:00+03:00'}
            self.assertEqual(rank_event(profile,event,date(2026,10,23))['business_score'],0)

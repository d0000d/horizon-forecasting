import asyncio
from copy import deepcopy
from datetime import timedelta
from unittest import TestCase
from unittest.mock import AsyncMock
from horizon.asknews_evidence import prepare_evidence, forecast_from_research, forecast_automatically
from test_horizon import Q, NOW


class EvidenceBridgeTests(TestCase):
    def setUp(self):
        self.record = dict(provider='AskNews', status='ok', completed_at=NOW.isoformat(),
                           response={'as_dicts': [dict(article_id='a', article_url='https://example.test/a',
                                                      summary='Relevant test summary', pub_date=None),
                                                 dict(article_id='b', article_url='https://example.test/b',
                                                      summary='Unrelated test summary', pub_date=None)]})

    def test_only_reviewed_articles_enter_forecast(self):
        evidence = prepare_evidence(self.record, Q, ['a'])
        self.assertEqual(len(evidence), 1)
        self.assertIn('not a verified publisher quotation', evidence[0].text)
        self.assertFalse(evidence[0].primary)
        self.assertIsNone(evidence[0].published_at)

    def test_future_research_and_publication_rejected(self):
        for mode in ('retrieval', 'publication'):
            record = deepcopy(self.record)
            future = (NOW+timedelta(seconds=1)).isoformat()
            if mode == 'retrieval':
                record['completed_at'] = future
            else:
                record['response']['as_dicts'][0]['pub_date'] = future
            with self.assertRaises(ValueError):
                prepare_evidence(record, Q, ['a'])

    def test_empty_unknown_duplicate_selection_rejected(self):
        for ids in ([], ['unknown'], ['a', 'a']):
            with self.assertRaises(ValueError):
                prepare_evidence(self.record, Q, ids)

    def test_empty_research_never_calls_council(self):
        council = AsyncMock()
        self.record['status'] = 'empty'
        with self.assertRaises(ValueError):
            asyncio.run(forecast_from_research(council, Q, self.record, ['a']))
        council.forecast.assert_not_called()

    def test_bridge_forwards_evidence(self):
        council = AsyncMock()
        asyncio.run(forecast_from_research(council, Q, self.record, ['a']))
        self.assertEqual(council.forecast.call_args.kwargs['evidence'][0].id, 'asknews-a')

    def test_automatic_bridge_requires_selector_and_forwards_candidates(self):
        council = AsyncMock()
        council.select_evidence = False
        with self.assertRaises(ValueError):
            asyncio.run(forecast_automatically(council, Q, self.record))
        council.forecast.assert_not_called()
        council.select_evidence = True
        asyncio.run(forecast_automatically(council, Q, self.record))
        self.assertEqual([e.id for e in council.forecast.call_args.kwargs['evidence']],
                         ['asknews-a', 'asknews-b'])

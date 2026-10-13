"""Search progress keeps page counts, full totals and missing coverage distinct."""
import unittest

from conversation_archive.mcp import _search_result


class SearchSummaryTests(unittest.TestCase):
    def test_page_filters_and_exact_query_without_transcript_copy(self):
        query = 'needle ' * 30
        page = {'provider': 'codex', 'items': [{'text': 'private transcript'}],
                'offset': 10, 'has_more': True, 'next_offset': 11}
        result = _search_result(page, query, role='user', start_date='2026-01-01')
        self.assertEqual(result['search_query'], query)
        self.assertEqual(result['search_filters'],
                         {'role': 'user', 'start_date': '2026-01-01', 'end_date': None})
        self.assertIn('1 messages returned at offset 10', result['search_summary'])
        self.assertIn('more available at offset 11', result['search_summary'])
        self.assertIn('role=user', result['search_summary'])
        self.assertIn('2026-01-01 through unbounded', result['search_summary'])
        self.assertNotIn('private transcript', result['search_summary'])
        self.assertEqual(result['items'], page['items'])

    def test_combined_unavailable_sources_and_unfiltered_historical_page(self):
        result = _search_result({'providers': [
            {'provider': 'codex', 'available': True, 'items': [], 'offset': 0, 'has_more': False},
            {'provider': 'claude', 'available': False}],
            'historical': {'available': True, 'items': [{}], 'offset': 0, 'has_more': False}},
            'needle', role='user')
        summary = result['search_summary']
        self.assertIn('codex: 0 messages returned', summary)
        self.assertIn('claude: query unavailable', summary)
        self.assertIn('historical: 1 documents returned', summary)
        self.assertIn('historical role/date filters not applied', summary)
        unavailable = _search_result({'providers': [], 'historical': {'available': False}}, 'needle')
        self.assertIn('historical: query unavailable', unavailable['search_summary'])

    def test_missing_scope_differs_from_empty_and_topic_totals_differ_from_page(self):
        page = {'provider': 'codex', 'conversation_id': 'missing', 'conversation_available': False,
                'items': [], 'offset': 0, 'has_more': False}
        self.assertIn('conversation absent from this index', _search_result(page, 'needle')['search_summary'])
        page['conversation_available'] = True
        self.assertIn('0 messages returned', _search_result(page, 'needle')['search_summary'])
        topic = {'provider': 'codex', 'items': [{}], 'offset': 0, 'has_more': True,
                 'next_offset': 1, 'matching_messages': 9, 'matching_conversations': 3}
        summary = _search_result(topic, 'needle')['search_summary']
        self.assertIn('1 conversations returned', summary)
        self.assertIn('9 matching messages across 3 conversations', summary)


if __name__ == '__main__':
    unittest.main()

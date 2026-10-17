import copy
import unittest
from home_acceptance import verify


class HomeAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.summary = {'org_id': 7, 'orgs': [{'id': 7}],
                        'widgets': {'tasks_by_status': {'status': 'ok', 'data': {'open': 1}}}}
        self.calls = []

    def api(self, method, path, token=None, expected=(200, 201)):
        self.calls.append((method, path, token, expected))
        return copy.deepcopy(self.summary)

    def test_actual_acceptance_caller_requires_authenticated_data_and_both_denials(self):
        verify(self.api, 'owner-fixture', 'outsider-fixture', 7, 9)
        self.assertEqual(self.calls, [
            ('GET', '/api/home/summary', None, (401, 403)),
            ('GET', '/api/home/summary?org_id=7', 'owner-fixture', (200, 201)),
            ('GET', '/api/home/summary?org_id=9', 'owner-fixture', (403,)),
            ('GET', '/api/home/summary?org_id=7', 'outsider-fixture', (403,)),
        ])

    def test_http200_empty_stale_or_foreign_scope_is_not_acceptance(self):
        pristine = copy.deepcopy(self.summary)
        for mutate in (
            lambda s: s.update(org_id=9),
            lambda s: s['orgs'].append({'id': 9}),
            lambda s: s.update(orgs=[]),
            lambda s: s['widgets'].update(tasks_by_status={'status': 'degraded', 'data': {}}),
            lambda s: s['widgets']['tasks_by_status'].update(status='stale'),
            lambda s: s['widgets']['tasks_by_status'].update(data={'open': 0}),
            lambda s: s['widgets']['tasks_by_status'].update(data={'open': True}),
        ):
            self.summary = copy.deepcopy(pristine)
            mutate(self.summary)
            with self.subTest(summary=self.summary), self.assertRaises(RuntimeError):
                verify(self.api, 'owner-fixture', 'outsider-fixture', 7, 9)

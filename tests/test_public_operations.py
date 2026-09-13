"""Synthetic-only portable collector checks for the public CI package.

These notices/results are constructed in memory, never written to production.
"""
import copy
import tempfile
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

from collector.auto_normalize import normalize
from collector.refresh import merge_notice, page_navigation, lifecycle, DEFAULT, select_details


def fixture(ratio=None):
    name = '가상시험 사무보조(기간제)'
    table = {'unit_name_raw': name,
             'official_ratio': {'label': '최종 경쟁률', 'value_raw': ratio, 'status': '미공개' if ratio is None else '확인'},
             'table_rows_raw': [[name], ['구분', '응시인원', '선발인원', '결과 확정일'],
                                ['서류', '20명' if ratio else '-', '5명' if ratio else '-', ''],
                                ['최종', '4명' if ratio else '-', '1명' if ratio else '-', '']]}
    detail = {'id': 'job-alio-900001', 'idx': '900001', 'org': '가상시험기관', 'title': '가상 시험 사무 채용',
              'announcement_period_raw': '2026.08.01 ~ 2026.08.15',
              'announcement_fields_raw': {'등록일': '2026.08.01', '채용인원': '9명', '고용형태': '비정규직', '근무지': '서울'},
              'application_period': {'raw': None},
              'sections': {'응시자격': '가상 시험 본문', '전형절차/방법': '가상 서류 및 면접'},
              'unit_results_raw': [table], 'institution_notice_urls': [], 'attachment_urls': []}
    ref = {'path': 'evidence/synthetic-not-a-source.body', 'url': 'https://job.alio.go.kr/recruitview.do?idx=900001',
           'sha256': '0' * 64, 'receivedAt': '2026-09-13T00:00:00+00:00'}
    return detail, ref


class PortableOperationsTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('offline only'))
        self.network.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()
        self.network.stop()

    def create(self, ratio=None):
        detail, ref = fixture(ratio)
        notice, _ = merge_notice(self.root, None, normalize(detail, ref), detail, ref)
        return notice

    def test_addition_without_ai_or_raw_files_uses_unit_scope(self):
        first = self.create()
        second = self.create()
        self.assertEqual(first['units'][0]['id'], second['units'][0]['id'])
        self.assertEqual(first['totalVacancies'], 9)
        self.assertIsNone(first['units'][0]['vacancies']['value'])
        self.assertIsNone(first['units'][0]['computer']['value'])

    def test_late_result_and_correction_preserve_fields(self):
        old = self.create()
        detail, ref = fixture('20')
        current, _ = merge_notice(self.root, old, normalize(detail, ref), detail, ref)
        self.assertEqual(current['units'][0]['competition']['published'], 20)
        detail['unit_results_raw'][0]['official_ratio']['value_raw'] = '21'
        corrected, _ = merge_notice(self.root, current, normalize(detail, ref), detail, ref)
        self.assertEqual(corrected['units'][0]['competition']['published'], 21)
        self.assertEqual(corrected['units'][0]['pay'], old['units'][0]['pay'])
        self.assertIsNone(corrected['units'][0]['vacancies']['value'])

    def test_partial_result_cannot_erase_previous_official_value(self):
        old = self.create('20')
        detail, ref = fixture('20')
        detail['unit_results_raw'][0]['table_rows_raw'][2][1] = '판독 불가(가상)'
        current, _ = merge_notice(self.root, old, normalize(detail, ref), detail, ref)
        self.assertEqual(current['units'][0]['competition'], old['units'][0]['competition'])
        self.assertEqual(current['autoSync']['resultCheck']['status'], 'partial')

    def test_page_pointer_and_closed_rechecks_survive_separate_state(self):
        raw = b'<a class="end" onclick="goPage(25)">end</a>'
        self.assertEqual(page_navigation(raw, 11)['nextPage'], 12)
        notice = self.create()
        row = {'id': notice['id'], 'idx': '900001', 'org': notice['organization'], 'title': notice['title']}
        state = {'inbox': {notice['id']: {'row': row, 'lastAttemptAt': '2026-09-10T00:00:00+00:00'}}}
        selected = select_details([], {'notices': [notice], 'asOfDate': '2026-09-13'}, state, DEFAULT)
        self.assertEqual(selected[0]['id'], notice['id'])
        self.assertEqual(lifecycle(notice, '2026-09-13T00:00:00+00:00')['status'], 'deadline_elapsed')

    def test_unprocessed_discovery_is_not_abandoned_when_it_closes(self):
        row = {'id': 'job-alio-900001', 'idx': '900001', 'title': '가상시험 사무원',
               'org': '가상시험기관', 'closing_raw': '26.08.15'}
        state = {'inbox': {row['id']: {'row': row, 'state': 'queued'}}}
        self.assertEqual(select_details([], {'notices': [], 'asOfDate': '2026-09-13'}, state, DEFAULT), [row])

    def test_closed_retry_cadence_uses_korean_calendar_date(self):
        notice = self.create()
        row = {'id': notice['id'], 'idx': '900001', 'title': notice['title'], 'org': notice['organization']}
        # UTC Sep12 23:00 is Korean Sep13 08:00: not a new Korean day yet.
        state = {'inbox': {notice['id']: {'row': row, 'lastAttemptAt': '2026-09-12T23:00:00+00:00'}}}
        self.assertEqual(select_details([], {'notices': [notice], 'asOfDate': '2026-09-13'}, state, DEFAULT), [])


if __name__ == '__main__':
    unittest.main()

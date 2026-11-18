"""本地来源与连续采集收据的回归。

覆盖这些容易出错的地方：

* 首版只公布总额时，缺项是“未知”，不能写零；
* 补发分类后当前事实更新，旧版本与出处仍在；
* 发布期之间不互相补齐；
* 正文撤回期间目录仍在不等于完整数据；
* 中断后续采能说清已核实/待确认，且与入库结果一致。
"""
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen

import app.pipeline as pipeline
import app.site as site
from app.capture import read_catalog, read_detail
from app.pipeline import collect, remaining, resume
from app.records import (
    connect,
    current_snapshot,
    list_receipts,
    progress_report,
    revision_history,
    save_catalog,
    save_detail,
)

TOTAL = 'total'
TOURISM = 'tourism_consulting'
SPORTS = 'culture_sports_leisure'


class SourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = site.start_server()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        site.clear_current()
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Path(self.temporary.name) / 'records.sqlite3'

    def tearDown(self):
        site.clear_current()
        self.temporary.cleanup()

    # ------------------------------------------------------------ 基础来源

    def test_catalog_and_detail(self):
        items = read_catalog(self.origin)['items']
        self.assertEqual(len(items), len(site.DATA['items']))
        for item in items:
            detail = read_detail(self.origin, item['id'])
            self.assertEqual(detail['item']['id'], item['id'])
            self.assertEqual(detail['item']['edition'], '1')

    def test_revision_and_page(self):
        key = site.DATA['items'][0]['id']
        self.assertNotEqual(
            read_detail(self.origin, key, '1')['item']['detail'],
            read_detail(self.origin, key, '2')['item']['detail'],
        )
        with urlopen(self.origin + '/catalog.html?edition=2') as response:
            self.assertIn(site.DATA['title'], response.read().decode('utf-8'))

    def test_receipts(self):
        catalog = read_catalog(self.origin)
        self.assertEqual(save_catalog(self.db, catalog), 2)
        save_detail(self.db, read_detail(self.origin, catalog['items'][0]['id']))
        self.assertEqual(len(list_receipts(self.db)), 1)

    def test_remote_rejected(self):
        with self.assertRaises(ValueError):
            read_catalog('https://example.com')

    def test_catalog_does_not_reject_legacy_row(self):
        # 旧资料没有 editions，也能被解释成两个发布期的不同版本。
        for edition in ('1', '2'):
            self.assertTrue(read_catalog(self.origin, edition)['items'])

    # ------------------------------------------------------------ 覆盖与零值

    def test_partial_first_release_is_not_zero(self):
        site.set_current('bulletin-a', '1', '1')
        save_detail(self.db, read_detail(self.origin, 'bulletin-a', '1'))
        snapshot = current_snapshot(self.db, 'bulletin-a', '1')
        self.assertEqual(snapshot, {TOTAL: 4.9})
        report = progress_report(self.db)
        page = next(p for p in report['verified'] if p['source_id'] == 'bulletin-a')
        self.assertFalse(page['complete'])
        self.assertEqual(page['missing'], [TOURISM, SPORTS])

    def test_supplement_updates_facts_and_keeps_history(self):
        site.set_current('bulletin-a', '1', '1')
        collect(self.db, self.origin, editions=('1',))
        site.set_current('bulletin-a', '1', '2')
        collect(self.db, self.origin, editions=('1',))
        snapshot = current_snapshot(self.db, 'bulletin-a', '1')
        # 补发后三个指标都有明确取值；文体休闲的 0 是真实零值。
        self.assertEqual(snapshot, {TOTAL: 4.9, TOURISM: 6.1, SPORTS: 0})
        history = revision_history(self.db, 'bulletin-a', '1')
        self.assertEqual([row['revision'] for row in history], ['1', '2'])
        self.assertIn('/detail?', history[0]['url'])
        old = next(
            row for row in list_receipts(self.db, 'bulletin-a', '1')
            if row['item']['revision'] == '1'
        )
        self.assertEqual(old['item']['facts'], {TOTAL: 4.9})

    def test_newer_revision_missing_fact_does_not_inherit(self):
        site.set_current('bulletin-a', '1', '2')
        save_detail(self.db, read_detail(self.origin, 'bulletin-a', '1'))
        # 修订版只公布总额：分类必须回到未知，不能沿用旧版本。
        result = read_detail(self.origin, 'bulletin-a', '1')
        result['item'] = dict(result['item'], revision='3', coverage=['total'],
                              facts={TOTAL: 4.95})
        result['revision'] = '3'
        save_detail(self.db, result)
        self.assertEqual(
            current_snapshot(self.db, 'bulletin-a', '1'),
            {TOTAL: 4.95},
        )

    def test_historical_read_does_not_regress_current_facts(self):
        site.set_current('bulletin-a', '1', '2')
        save_detail(self.db, read_detail(self.origin, 'bulletin-a', '1'))
        save_detail(self.db, read_detail(self.origin, 'bulletin-a', '1', revision='1'))
        self.assertEqual(
            current_snapshot(self.db, 'bulletin-a', '1'),
            {TOTAL: 4.9, TOURISM: 6.1, SPORTS: 0},
        )
        self.assertEqual(len(list_receipts(self.db, 'bulletin-a', '1')), 2)

    # ------------------------------------------------------------ 发布期隔离

    def test_editions_never_share_facts(self):
        collect(self.db, self.origin, editions=('1', '2'))
        edition_one = current_snapshot(self.db, 'bulletin-b', '1')
        edition_two = current_snapshot(self.db, 'bulletin-b', '2')
        self.assertEqual(edition_one, {TOTAL: 5.3, TOURISM: 2.2, SPORTS: 1.1})
        self.assertEqual(edition_two, {TOTAL: 4.4, TOURISM: 3.3})
        # 2 期的文体休闲仍未公布，不能被 1 期补齐。
        self.assertNotIn(SPORTS, edition_two)

    def test_edition_two_is_reported_incomplete(self):
        collect(self.db, self.origin, editions=('1', '2'))
        report = progress_report(self.db)
        page = next(
            p for p in report['verified']
            if p['source_id'] == 'bulletin-a' and p['edition'] == '2'
        )
        self.assertFalse(page['complete'])
        self.assertEqual(page['missing'], [TOURISM, SPORTS])

    # ------------------------------------------------------------ 撤回与续采

    def test_withdrawn_body_is_not_data(self):
        collect(self.db, self.origin, editions=('1',))
        self.assertEqual(
            current_snapshot(self.db, 'bulletin-b', '1'),
            {TOTAL: 5.3, TOURISM: 2.2, SPORTS: 1.1},
        )
        site.set_current('bulletin-b', '1', '2')
        run_id, report = collect(self.db, self.origin, editions=('1',))
        self.assertEqual(current_snapshot(self.db, 'bulletin-b', '1'), {})
        page = next(p for p in report['pending'] if p['source_id'] == 'bulletin-b')
        self.assertEqual(page['detail_status'], 'withdrawn')
        # 目录仍在，但这一页在本次批次里明确是待确认。
        self.assertIn('bulletin-b', [entry['id'] for entry in read_catalog(self.origin)['items']])
        self.assertEqual(len(revision_history(self.db, 'bulletin-b', '1')), 2)

    def test_resume_fills_pending_after_republish(self):
        site.set_current('bulletin-b', '1', '2')
        collect(self.db, self.origin, editions=('1',))
        pending = remaining(self.db)
        self.assertEqual([p['source_id'] for p in pending['pending']], ['bulletin-b'])
        site.set_current('bulletin-b', '1', '3')
        run_id, report = resume(self.db, self.origin)
        self.assertEqual(report['pending'], [])
        self.assertEqual(
            current_snapshot(self.db, 'bulletin-b', '1'),
            {TOTAL: 5.3, TOURISM: 2.2, SPORTS: 1.1},
        )

    def test_interrupted_run_reports_verified_and_pending(self):
        real = pipeline.read_detail
        calls = {'count': 0}

        def interrupt_on_second(*args, **kwargs):
            calls['count'] += 1
            if calls['count'] == 2:
                raise KeyboardInterrupt('operator stop')
            return real(*args, **kwargs)

        pipeline.read_detail = interrupt_on_second
        try:
            with self.assertRaises(KeyboardInterrupt):
                collect(self.db, self.origin, editions=('1',))
        finally:
            pipeline.read_detail = real

        report = progress_report(self.db)
        self.assertEqual(report['run_state'], 'running')
        self.assertEqual([p['source_id'] for p in report['verified']], ['bulletin-a'])
        self.assertEqual([p['source_id'] for p in report['pending']], ['bulletin-b'])
        # 进度与入库结果一致：已核实页面确有收据与事实，待确认页面没有。
        connection = connect(self.db)
        try:
            for page in report['verified']:
                receipt = connection.execute(
                    'SELECT source_id FROM details WHERE receipt=?', (page['receipt'],)
                ).fetchone()
                self.assertIsNotNone(receipt)
                self.assertTrue(current_snapshot(self.db, page['source_id'], page['edition']))
            for page in report['pending']:
                self.assertEqual(current_snapshot(self.db, page['source_id'], page['edition']), {})
        finally:
            connection.close()

        run_id, report = resume(self.db, self.origin)
        self.assertEqual(report['run_state'], 'finished')
        self.assertEqual(report['pending'], [])
        self.assertEqual(report['verified_count'], 2)

    def test_verified_pages_not_reread_on_resume(self):
        site.set_current('bulletin-b', '1', '2')
        run_id, first = collect(self.db, self.origin, editions=('1',))
        read = {'count': 0}
        real = pipeline.read_detail

        def counting(*args, **kwargs):
            read['count'] += 1
            return real(*args, **kwargs)

        pipeline.read_detail = counting
        try:
            site.set_current('bulletin-b', '1', '3')
            resume(self.db, self.origin)
        finally:
            pipeline.read_detail = real
        # 只重读仍待确认的 bulletin-b，不重读已核实的 bulletin-a。
        self.assertEqual(read['count'], 1)


if __name__ == '__main__':
    unittest.main()

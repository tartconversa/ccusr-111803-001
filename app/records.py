"""保存原始收据、当前可用事实与采集进度。

设计要点：

* 每次读取目录/正文都留一条原始收据，含 URL、发布期、发布版本与原始页；
* 当前事实按（目录标识，发布期）保存，只来自发布方此刻的当前页；
  不同发布期绝不合并，缺失指标不会被补成零或借用别的发布期；
* 明示的 0 是真实事实，会照常保存；没有取值的指标是“未知”；
* 正文被撤回时，当前事实清空并标记为待确认，但历史收据保留；
* 收据、当前事实与进度在同一事务提交；进度报告以库内事实与收据为准，
  不会声称某页已核实而实际没有入库结果。
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .domain import REQUIRED_FACTS, revision_key

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS catalog_receipts (
        receipt INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id TEXT NOT NULL,
        edition TEXT NOT NULL,
        url TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        status TEXT,
        run_id TEXT,
        payload TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS details (
        receipt INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id TEXT NOT NULL,
        edition TEXT NOT NULL,
        revision TEXT NOT NULL,
        url TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        status TEXT NOT NULL,
        is_current INTEGER NOT NULL DEFAULT 1,
        run_id TEXT,
        payload TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS detail_facts (
        receipt INTEGER NOT NULL,
        source_id TEXT NOT NULL,
        edition TEXT NOT NULL,
        revision TEXT NOT NULL,
        metric TEXT NOT NULL,
        value,
        PRIMARY KEY (receipt, metric)
    )""",
    """CREATE TABLE IF NOT EXISTS current_facts (
        source_id TEXT NOT NULL,
        edition TEXT NOT NULL,
        metric TEXT NOT NULL,
        value,
        revision TEXT NOT NULL,
        receipt INTEGER NOT NULL,
        url TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (source_id, edition, metric)
    )""",
    """CREATE TABLE IF NOT EXISTS progress (
        source_id TEXT NOT NULL,
        edition TEXT NOT NULL,
        page_state TEXT NOT NULL,
        detail_status TEXT,
        revision TEXT,
        required TEXT,
        missing TEXT,
        coverage TEXT,
        receipt INTEGER,
        url TEXT,
        run_id TEXT,
        note TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (source_id, edition)
    )""",
    """CREATE TABLE IF NOT EXISTS catalog_progress (
        edition TEXT NOT NULL,
        run_id TEXT,
        url TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        item_count INTEGER NOT NULL,
        PRIMARY KEY (edition, run_id)
    )""",
    """CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        origin TEXT,
        editions TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        state TEXT NOT NULL
    )""",
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    for statement in SCHEMA:
        db.execute(statement)
    db.commit()
    return db


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _revision_key(revision):
    return revision_key(revision)


def _facts(item):
    """只保留有明确取值的指标；显式 0 会被保留。"""
    return {
        str(key): value
        for key, value in (item.get('facts') or {}).items()
        if value is not None
    }


def _required(item):
    keys = item.get('required_coverage') or item.get('required_facts') or REQUIRED_FACTS
    return tuple(str(key) for key in keys)


def _coverage(item):
    return tuple(str(key) for key in (item.get('coverage') or ()))


def _missing(item):
    """必需但没有明确取值（或未声明在覆盖范围内）的指标。"""
    facts = _facts(item)
    declared = set(_coverage(item))
    return tuple(
        key for key in _required(item) if key not in facts or key not in declared
    )


def _is_published(item):
    return item.get('status', 'published') == 'published'


# ---------------------------------------------------------------- 采集批次

def start_run(path, origin, editions):
    """登记一次采集；返回可续采用的批次标识。"""
    editions = tuple(str(edition) for edition in editions)
    run_id = uuid4().hex
    db = connect(path)
    try:
        db.execute(
            'INSERT INTO runs (run_id, origin, editions, started_at, state) VALUES (?,?,?,?,?)',
            (run_id, origin, _json(editions), _now(), 'running'),
        )
        db.commit()
    finally:
        db.close()
    return run_id


def finish_run(path, run_id, state='finished'):
    db = connect(path)
    try:
        db.execute(
            'UPDATE runs SET finished_at=?, state=? WHERE run_id=?',
            (_now(), state, run_id),
        )
        db.commit()
    finally:
        db.close()


def get_run(path, run_id):
    db = connect(path)
    try:
        row = db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
    finally:
        db.close()
    if row is None:
        return None
    result = dict(row)
    result['editions'] = json.loads(result['editions'])
    return result


def latest_run(path, unfinished_only=False):
    query = 'SELECT run_id FROM runs'
    if unfinished_only:
        query += " WHERE state='running'"
    query += ' ORDER BY started_at DESC, rowid DESC LIMIT 1'
    db = connect(path)
    try:
        row = db.execute(query).fetchone()
    finally:
        db.close()
    return row['run_id'] if row else None


def latest_finished_run(path):
    db = connect(path)
    try:
        row = db.execute(
            "SELECT run_id FROM runs WHERE state='finished' "
            'ORDER BY started_at DESC, rowid DESC LIMIT 1'
        ).fetchone()
    finally:
        db.close()
    return row['run_id'] if row else None


# ---------------------------------------------------------------- 原始收据

def save_catalog(path, result, run_id=None):
    """保存一次目录页读取。目录只用于定位与计划，从不提供事实值。"""
    edition = str(result['edition'])
    url = result['url']
    fetched_at = result.get('fetched_at') or _now()
    raw = result.get('raw')
    db = connect(path)
    try:
        for item in result['items']:
            db.execute(
                'INSERT INTO catalog_receipts '
                '(source_id, edition, url, fetched_at, status, run_id, payload) '
                'VALUES (?,?,?,?,?,?,?)',
                (
                    item['id'],
                    edition,
                    url,
                    fetched_at,
                    item.get('status'),
                    run_id,
                    _json(item),
                ),
            )
        if raw is not None:
            # 目录原始页单独留痕，供核对“目录仍在但正文已撤回”的情形。
            db.execute(
                'INSERT INTO catalog_receipts '
                '(source_id, edition, url, fetched_at, status, run_id, payload) '
                'VALUES (?,?,?,?,?,?,?)',
                ('*', edition, url, fetched_at, 'catalog', run_id, raw),
            )
        db.execute(
            'INSERT OR REPLACE INTO catalog_progress '
            '(edition, run_id, url, fetched_at, item_count) VALUES (?,?,?,?,?)',
            (edition, run_id, url, fetched_at, len(result['items'])),
        )
        db.commit()
    finally:
        db.close()
    return len(result['items'])


def _write_progress(db, item, receipt, url, run_id, updated_at, note=None,
                    page_state='verified'):
    db.execute(
        'INSERT OR REPLACE INTO progress '
        '(source_id, edition, page_state, detail_status, revision, required, '
        ' missing, coverage, receipt, url, run_id, note, updated_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            str(item['id']),
            str(item['edition']),
            page_state,
            item.get('status', 'published'),
            str(item['revision']),
            _json(list(_required(item))),
            _json(list(_missing(item))),
            _json(list(_coverage(item))),
            receipt,
            url,
            run_id,
            note,
            updated_at,
        ),
    )


def save_detail(path, result, run_id=None, note=None):
    """保存一次正文读取，并只让该发布期的“当前页”更新当前事实。

    * 当前页（``is_current``）是发布方此刻的结论，即使版本号回退也以它为准；
    * 显式回读的历史版本只追加收据，绝不改写当前事实；
    * 旧版本收据永久保留，含 URL、发布期、版本和原始页；
    * 撤回期间目录条目仍在，当前事实清空并标为待确认；
    * 若新版本缺少某分类，该分类回到“未知”，不会沿用旧版本或写成零。
    """
    item = result['item']
    source_id = str(item['id'])
    edition = str(item['edition'])
    revised = str(item['revision'])
    status = item.get('status', 'published')
    url = result['url']
    fetched_at = result.get('fetched_at') or _now()
    is_current = result.get('is_current')
    db = connect(path)
    try:
        cursor = db.execute(
            'INSERT INTO details '
            '(source_id, edition, revision, url, fetched_at, status, is_current, '
            ' run_id, payload) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (
                source_id,
                edition,
                revised,
                url,
                fetched_at,
                status,
                0 if is_current is False else 1,
                run_id,
                _json(result),
            ),
        )
        receipt = cursor.lastrowid
        for metric, value in _facts(item).items():
            db.execute(
                'INSERT INTO detail_facts '
                '(receipt, source_id, edition, revision, metric, value) '
                'VALUES (?,?,?,?,?,?)',
                (receipt, source_id, edition, revised, metric, value),
            )

        current = db.execute(
            'SELECT * FROM progress WHERE source_id=? AND edition=?',
            (source_id, edition),
        ).fetchone()
        current_revision = current['revision'] if current else None
        current_status = current['detail_status'] if current else None

        if is_current is False:
            # 显式回读历史版本：只留收据。
            should_apply = False
        elif is_current is None and current_revision is not None:
            # 调用方没有说明这是否为当前页：按版本单调性保守处理。
            should_apply = _revision_key(revised) >= _revision_key(current_revision)
            if should_apply and _revision_key(revised) == _revision_key(current_revision):
                should_apply = _content_changed(db, source_id, edition, revised,
                                                receipt, item, status,
                                                current_status)
        else:
            # 当前页是发布方此刻的结论；同一版本内容变化也视为修订。
            should_apply = True

        if should_apply:
            # 当前事实只来自当前页，不跨版本、不跨发布期合并。
            db.execute(
                'DELETE FROM current_facts WHERE source_id=? AND edition=?',
                (source_id, edition),
            )
            if _is_published(item):
                for metric, value in _facts(item).items():
                    db.execute(
                        'INSERT INTO current_facts '
                        '(source_id, edition, metric, value, revision, receipt, url, updated_at) '
                        'VALUES (?,?,?,?,?,?,?,?)',
                        (source_id, edition, metric, value, revised, receipt, url, fetched_at),
                    )
            # 撤回时正文不可用：当前事实已清空，页面待重新挂出后才算核实。
            _write_progress(
                db,
                item,
                receipt,
                url,
                run_id,
                fetched_at,
                note=note,
                page_state='verified' if _is_published(item) else 'pending',
            )
        elif is_current is not False and _is_published(item) and (
            current is None or current['page_state'] != 'verified' or (
                run_id is not None and current['run_id'] != run_id
            )
        ):
            # 当前页内容未变但本次已成功核实：进度指向本次收据，事实不动。
            _write_progress(db, item, receipt, url, run_id, fetched_at, note=note)
        db.commit()
    finally:
        db.close()
    return source_id


def _content_changed(db, source_id, edition, revised, receipt, item, status,
                     current_status):
    previous = db.execute(
        'SELECT payload FROM details WHERE source_id=? AND edition=? '
        'AND revision=? AND receipt<? ORDER BY receipt DESC LIMIT 1',
        (source_id, edition, revised, receipt),
    ).fetchone()
    if previous is None:
        return True
    previous_item = json.loads(previous['payload']).get('item', {})
    return (
        previous_item.get('facts') != item.get('facts')
        or previous_item.get('coverage') != item.get('coverage')
        or previous_item.get('status') != status
        or current_status != status
    )


def mark_pending(path, source_id, edition, run_id=None, note=None):
    """记录一个仍待确认的页面。

    只更新进度，不写入或清除任何事实：读取失败不代表旧事实失效，但这一页
    在本次采集中不能算已核实。
    """
    updated_at = _now()
    db = connect(path)
    try:
        current = db.execute(
            'SELECT * FROM progress WHERE source_id=? AND edition=?',
            (str(source_id), str(edition)),
        ).fetchone()
        db.execute(
            'UPDATE progress SET page_state=?, run_id=?, note=?, updated_at=? '
            'WHERE source_id=? AND edition=?',
            ('pending', run_id, note, updated_at, str(source_id), str(edition)),
        )
        if current is None:
            db.execute(
                'INSERT INTO progress '
                '(source_id, edition, page_state, detail_status, revision, required, '
                ' missing, coverage, receipt, url, run_id, note, updated_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (
                    str(source_id),
                    str(edition),
                    'pending',
                    None,
                    None,
                    _json(list(REQUIRED_FACTS)),
                    _json(list(REQUIRED_FACTS)),
                    '[]',
                    None,
                    None,
                    run_id,
                    note,
                    updated_at,
                ),
            )
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------- 查询

def list_receipts(path, source_id=None, edition=None):
    db = connect(path)
    try:
        query = 'SELECT payload FROM details'
        clauses = []
        values = []
        if source_id is not None:
            clauses.append('source_id=?')
            values.append(str(source_id))
        if edition is not None:
            clauses.append('edition=?')
            values.append(str(edition))
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY receipt'
        return [json.loads(row['payload']) for row in db.execute(query, values)]
    finally:
        db.close()


def list_catalog_receipts(path, edition=None):
    db = connect(path)
    try:
        query = 'SELECT * FROM catalog_receipts'
        values = []
        if edition is not None:
            query += ' WHERE edition=?'
            values.append(str(edition))
        query += ' ORDER BY receipt'
        return [
            {
                'receipt': row['receipt'],
                'source_id': row['source_id'],
                'edition': row['edition'],
                'url': row['url'],
                'fetched_at': row['fetched_at'],
                'status': row['status'],
                'run_id': row['run_id'],
                'payload': row['payload'],
            }
            for row in db.execute(query, values)
        ]
    finally:
        db.close()


def revision_history(path, source_id, edition):
    """某发布期某条目读到的全部版本及其出处，按读取顺序。"""
    db = connect(path)
    try:
        return [
            {
                'receipt': row['receipt'],
                'revision': row['revision'],
                'status': row['status'],
                'url': row['url'],
                'fetched_at': row['fetched_at'],
                'run_id': row['run_id'],
            }
            for row in db.execute(
                'SELECT * FROM details WHERE source_id=? AND edition=? ORDER BY receipt',
                (str(source_id), str(edition)),
            )
        ]
    finally:
        db.close()


def current_facts(path, source_id=None, edition=None):
    """当前可用事实；只返回该发布期最新版本明确给出的值（含 0）。"""
    db = connect(path)
    try:
        query = 'SELECT * FROM current_facts'
        clauses = []
        values = []
        if source_id is not None:
            clauses.append('source_id=?')
            values.append(str(source_id))
        if edition is not None:
            clauses.append('edition=?')
            values.append(str(edition))
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY source_id, edition, metric'
        return [dict(row) for row in db.execute(query, values)]
    finally:
        db.close()


def current_snapshot(path, source_id, edition):
    """指标到当前值的映射：缺项就是缺项，不会补零。"""
    return {
        row['metric']: row['value']
        for row in current_facts(path, source_id, edition)
    }


def _edition_items(db, edition):
    """某发布期需要交代的页面：目录见到的、进度记过的、收据读过的都要算。"""
    return [
        row['source_id']
        for row in db.execute(
            'SELECT source_id FROM catalog_receipts '
            'WHERE edition=? AND source_id<>"*" '
            'UNION SELECT source_id FROM details WHERE edition=? '
            'UNION SELECT source_id FROM progress WHERE edition=? '
            'ORDER BY source_id',
            (str(edition), str(edition), str(edition)),
        )
    ]


def _known_editions(db):
    return [
        row['edition']
        for row in db.execute(
            'SELECT edition FROM catalog_progress '
            'UNION SELECT edition FROM details '
            'UNION SELECT edition FROM progress '
            'ORDER BY edition'
        )
    ]


def _stored_receipt(db, source_id, edition):
    """最近一次“当前页”读取；回读历史版本不参与核实判定。"""
    return db.execute(
        'SELECT * FROM details WHERE source_id=? AND edition=? AND is_current=1 '
        'ORDER BY receipt DESC LIMIT 1',
        (str(source_id), str(edition)),
    ).fetchone()


def _report_page(db, source_id, edition, run_id=None):
    row = db.execute(
        'SELECT * FROM progress WHERE source_id=? AND edition=?',
        (str(source_id), str(edition)),
    ).fetchone()
    stored = _stored_receipt(db, source_id, edition)
    facts = current_snapshot_in(db, source_id, edition)
    verified = (
        row is not None
        and row['page_state'] == 'verified'
        and stored is not None
        and stored['receipt'] == row['receipt']
        and stored['status'] == 'published'
        # 指定批次时，只把本批次已核实的页面算作已核实；
        # 上一批次留下的进度在本批次里仍是待确认。
        and (run_id is None or row['run_id'] == run_id)
    )
    required = json.loads(row['required']) if row and row['required'] else []
    if not required and stored is not None:
        stored_item = json.loads(stored['payload']).get('item', {})
        required = list(
            stored_item.get('required_coverage') or stored_item.get('required_facts') or ()
        )
    coverage = json.loads(row['coverage']) if row and row['coverage'] else []
    if verified:
        # 已核实：缺项以当前事实为准，未公布就是缺项，不补零。
        missing = [key for key in required if key not in facts]
    elif row is not None and row['missing']:
        missing = json.loads(row['missing'])
    else:
        missing = list(required or REQUIRED_FACTS)
    complete = verified and not missing and bool(required)
    return {
        'source_id': source_id,
        'edition': str(edition),
        'state': 'verified' if verified else 'pending',
        'detail_status': row['detail_status'] if row else None,
        'revision': row['revision'] if row else (stored['revision'] if stored else None),
        'complete': complete,
        'missing': missing,
        'coverage': coverage,
        'url': row['url'] if row else (stored['url'] if stored else None),
        'receipt': row['receipt'] if row else (stored['receipt'] if stored else None),
        'note': row['note'] if row else None,
        'current_facts': facts,
    }


def current_snapshot_in(db, source_id, edition):
    return {
        row['metric']: row['value']
        for row in db.execute(
            'SELECT metric, value FROM current_facts WHERE source_id=? AND edition=?',
            (str(source_id), str(edition)),
        )
    }


def progress_report(path, run_id=None):
    """说清哪些页面已核实、哪些仍待确认，并与库内事实/收据对账。

    “已核实”必须同时满足：进度记录存在、详情收据存在、两者指向同一收据。
    因此任何未完成入库的页面都不会被误报为已核实。
    """
    db = connect(path)
    try:
        if run_id is None:
            # 默认报告最近一次采集；若这次采集被中断，状态仍是 running，
            # 报告会如实显示哪些页已核实、哪些还待确认。
            run_id = latest_run(path) or latest_finished_run(path)
        run = db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone() if run_id else None
        if run is not None:
            editions = json.loads(run['editions'])
        else:
            editions = _known_editions(db)
        pages = []
        for edition in editions:
            for source_id in _edition_items(db, edition):
                pages.append(_report_page(db, source_id, edition, run['run_id'] if run else None))
        verified = [page for page in pages if page['state'] == 'verified']
        pending = [page for page in pages if page['state'] != 'verified']
        return {
            'run_id': run['run_id'] if run is not None else run_id,
            'run_state': run['state'] if run is not None else None,
            'editions': editions,
            'verified': verified,
            'pending': pending,
            'verified_count': len(verified),
            'pending_count': len(pending),
            'complete_count': sum(1 for page in verified if page['complete']),
        }
    finally:
        db.close()

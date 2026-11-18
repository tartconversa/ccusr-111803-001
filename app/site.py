"""供采集程序读取的本地演练来源。

* 目录（/catalog）只提供定位信息与“正文当前是否存在”，不提供事实值；
* 详情（/detail）按发布期（edition）返回该期最新版本（revision），
  可用 ``revision`` 参数回读历史版本；
* 发布方撤回正文时，最新版本状态为 ``withdrawn``、正文为空，但目录
  条目仍然存在——此时不能把条目当成完整数据；
* 不同发布期的分类互不共享，缺少的指标就是未知，不是零。
"""
import json
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .domain import REQUIRED_FACTS, revision_key

DATA = json.loads(
    (Path(__file__).resolve().parents[1] / 'fixtures' / 'source.json').read_text(encoding='utf-8')
)

DEFAULT_REQUIRED = list(REQUIRED_FACTS)

# 演练“发布时间差”：默认当前版本是最新版本，测试可把某一期退回旧版本，
# 模拟先公布总额、稍后补发分类，或先撤正文、再重新挂出。
_PUBLISHED = {}


def set_current(item_id, edition, revision):
    """把某条目某发布期的“当前版本”固定为指定版本。"""
    _PUBLISHED[(str(item_id), str(edition))] = str(revision)


def clear_current():
    _PUBLISHED.clear()


def editions_of(item):
    """旧资料只给出一份 detail/changed_detail，视为发布期 1/2 的两个版本。"""
    if 'editions' in item:
        return {str(edition): rows for edition, rows in item['editions'].items()}
    return {
        '1': [
            {
                'revision': '1',
                'status': 'published',
                'required_coverage': DEFAULT_REQUIRED,
                'coverage': DEFAULT_REQUIRED,
                'detail': item['detail'],
            }
        ],
        '2': [
            {
                'revision': '2',
                'status': 'published',
                'required_coverage': DEFAULT_REQUIRED,
                'coverage': DEFAULT_REQUIRED,
                'detail': item['changed_detail'],
            }
        ],
    }


def revisions(item, edition):
    return editions_of(item).get(str(edition), [])


def current_revision(item, edition):
    rows = revisions(item, edition)
    if not rows:
        return None
    pinned = _PUBLISHED.get((str(item['id']), str(edition)))
    if pinned is not None:
        return next(
            (row for row in rows if str(row['revision']) == pinned),
            None,
        )
    return max(rows, key=lambda row: revision_key(row['revision']))


def select_revision(item, edition, revision=None):
    if revision is None:
        return current_revision(item, edition)
    return next((row for row in revisions(item, edition) if str(row['revision']) == str(revision)), None)


def _detail_text(item, edition, row):
    if row.get('detail') is not None:
        return row['detail']
    if str(edition) == '2':
        return item.get('changed_detail', item.get('detail', ''))
    return item.get('detail', '')


def catalog(edition):
    items = []
    for item in DATA['items']:
        row = current_revision(item, edition)
        items.append({
            'id': item['id'],
            'name': item['name'],
            'group': item['group'],
            'summary': _detail_text(item, edition, row) if row else '',
            # 目录仍在不等于数据完整：这里只说明正文是否挂出。
            'status': row['status'] if row else 'unavailable',
        })
    return {'edition': edition, 'items': items}


def detail(item, edition, revision=None):
    row = select_revision(item, edition, revision)
    if row is None:
        return None
    return {
        'id': item['id'],
        'name': item['name'],
        'group': item['group'],
        'edition': edition,
        'revision': str(row['revision']),
        'status': row.get('status', 'published'),
        'required_coverage': list(row.get('required_coverage') or DEFAULT_REQUIRED),
        'coverage': list(row.get('coverage') or ()),
        'facts': dict(row.get('facts') or {}),
        'detail': _detail_text(item, edition, row),
    }


def catalog_page(edition):
    cards = ''.join(
        '<article data-id="' + escape(item['id']) + '" data-status="' +
        escape(catalog(edition)['items'][index]['status']) + '"><h2>' +
        escape(item['name']) + '</h2><a href="/detail.html?id=' + escape(item['id']) +
        '&edition=' + edition + '">查看详情</a></article>'
        for index, item in enumerate(DATA['items'])
    )
    return ('<html lang="zh"><meta charset="utf-8"><h1>' + escape(DATA['title']) +
            '</h1><main data-edition="' + edition + '">' + cards + '</main></html>')


def detail_page(item, edition, revision=None):
    info = detail(item, edition, revision)
    if info is None:
        return None
    body = escape(info['detail']) if info['status'] == 'published' else '正文已撤回，等待重新挂出'
    return ('<html lang="zh"><meta charset="utf-8"><h1>' + escape(item['name']) +
            '</h1><main data-id="' + escape(item['id']) + '" data-edition="' + edition +
            '" data-revision="' + escape(info['revision']) + '" data-status="' +
            escape(info['status']) + '"><label>' + escape(DATA['choice']) +
            '<select id="choice"><option>甲</option><option>乙</option></select></label>' +
            '<button id="show">查看' + escape(DATA['detail_label']) + '</button>' +
            '<section id="result">' + body + '</section><p>' + escape(DATA['revision']) +
            ' ' + escape(info['revision']) + '（' + escape(info['status']) + '）</p></main>' +
            '<script>const c=document.querySelector("#choice"),r=document.querySelector' +
            '("#result");c.onchange=()=>r.textContent="";document.querySelector("#show")' +
            '.onclick=()=>{r.textContent="读取中";let selected=c.value;setTimeout(()=>' +
            'r.textContent=selected+":"+' + json.dumps(info['detail'], ensure_ascii=False) +
            ',50)}</script></html>')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        args = parse_qs(url.query)
        edition = args.get('edition', ['1'])[0]
        revision = args.get('revision', [None])[0]
        if edition not in ('1', '2'):
            self.send_error(400)
            return
        if url.path in ('/catalog', '/catalog.html'):
            body = catalog_page(edition) if url.path.endswith('.html') else json.dumps(catalog(edition), ensure_ascii=False)
        elif url.path in ('/detail', '/detail.html'):
            item = next((x for x in DATA['items'] if x['id'] == args.get('id', [''])[0]), None)
            if item is None:
                self.send_error(404)
                return
            info = detail(item, edition, revision)
            if info is None:
                self.send_error(404)
                return
            body = detail_page(item, edition, revision) if url.path.endswith('.html') else json.dumps(info, ensure_ascii=False)
        else:
            self.send_error(404)
            return
        raw = body.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', ('text/html' if url.path.endswith('.html') else 'application/json') + '; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


def start_server():
    return ThreadingHTTPServer(('127.0.0.1', 0), Handler)


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', 8765), Handler)
    print('本地来源：http://127.0.0.1:8765/catalog', flush=True)
    server.serve_forever()

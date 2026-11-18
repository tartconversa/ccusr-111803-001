"""从本地来源获取目录和单项详情。

正文读取会保留发布期与发布版本；页面被撤回时返回的状态仍是原始页给出的
状态，由调用方决定是否把它当作完整数据。目录只用于定位条目，不提供事实。
"""
import json
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

from .domain import interpret_catalog, interpret_detail

ALLOWED_HOSTS = ('127.0.0.1', 'localhost')


def get(origin, path, query):
    url = urlsplit(origin)
    if url.scheme != 'http' or url.hostname not in ALLOWED_HOSTS:
        raise ValueError('只允许本地演练来源')
    address = origin.rstrip('/') + path + '?' + urlencode(query)
    with urlopen(address, timeout=5) as response:
        raw = response.read().decode('utf-8')
    return address, json.loads(raw), raw


def read_catalog(origin, edition='1'):
    address, data, raw = get(origin, '/catalog', {'edition': edition})
    items = []
    for item in data['items']:
        identity = interpret_catalog(item)
        items.append(
            dict(
                item,
                id=identity['source_id'],
                group=identity['group'],
                identity=identity,
                # 目录状态只是正文当前是否存在，不表示覆盖完整。
                status=item.get('status', 'unknown'),
            )
        )
    return {
        'url': address,
        'edition': str(data['edition']),
        'items': items,
        'raw': raw,
    }


def read_detail(origin, item_id, edition='1', revision=None):
    query = {'id': item_id, 'edition': edition}
    if revision is not None:
        query['revision'] = str(revision)
    address, data, raw = get(origin, '/detail', query)
    identity = interpret_detail(data)
    item = dict(
        data,
        id=identity['source_id'],
        group=identity['group'],
        edition=identity['edition'],
        revision=identity['revision'],
        status=data.get('status', 'published'),
        coverage=list(data.get('coverage') or ()),
        facts=dict(data.get('facts') or {}),
        required_coverage=list(data.get('required_coverage') or ()),
    )
    return {
        'url': address,
        'edition': identity['edition'],
        'revision': identity['revision'],
        # 未指定 revision 时读到的是发布方当前页；显式指定则是回读历史版本。
        'is_current': revision is None,
        'requested_revision': None if revision is None else str(revision),
        'raw': raw,
        'item': item,
    }

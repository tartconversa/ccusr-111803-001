"""连续采集：目录定位、逐页核实、按发布期保存当前事实与进度。

关键规则：

* 目录只用于定位；条目仍在目录中不代表正文完整，甚至不代表正文存在。
* 每个发布期单独保存事实，绝不拿另一发布期的分类补齐本月。
* 详情正文明确写出的 0 是真实零值；正文没有的指标是“未知”，不会补零。
* 补发或修订会更新当前可用事实；旧版本与旧出处作为收据永久保留。
* 每次读取详情后立即提交事实与进度；中断后 ``progress_report`` 能说清
  已核实/待确认，且入库结果与该进度一致。
"""
from .capture import read_catalog, read_detail
from .records import (
    finish_run,
    get_run,
    latest_run,
    mark_pending,
    progress_report,
    save_catalog,
    save_detail,
    start_run,
)


def _verify(path, origin, source_id, edition, run_id):
    """核实单个页面；失败时明确记为待确认。"""
    try:
        result = read_detail(origin, source_id, edition)
    except Exception:
        # 详情暂时不可用：目录仍在，但正文未经核实，不能算完整数据。
        mark_pending(path, source_id, edition, run_id=run_id, note='正文读取失败')
        return
    item = result['item']
    if item.get('status') != 'published':
        # 撤回期间目录仍在，但正文不存在：保存原始页；save_detail 会清空
        # 当前事实并把页面标为待确认，等待重新挂出后再核实。
        save_detail(path, result, run_id=run_id, note='正文已撤回，等待重新挂出')
        return
    save_detail(path, result, run_id=run_id)


def _catalog_pages(origin, edition, run_id, path):
    catalog = read_catalog(origin, edition)
    save_catalog(path, catalog, run_id=run_id)
    return [entry['id'] for entry in catalog['items']]


def collect(path, origin, editions=('1',), run_id=None):
    """采集指定发布期的全部目录条目。

    返回 ``run_id`` 与进度报告（以库内收据与事实为准）。
    """
    editions = tuple(str(edition) for edition in editions)
    if run_id is None:
        run_id = start_run(path, origin, editions)
    for edition in editions:
        # 每期重新读目录，才知道本期有哪些条目；不跨期借用条目或分类。
        for source_id in _catalog_pages(origin, edition, run_id, path):
            _verify(path, origin, source_id, str(edition), run_id)
    finish_run(path, run_id, state='finished')
    return run_id, progress_report(path, run_id=run_id)


def resume(path, origin, editions=None, run_id=None):
    """续采：补齐仍待确认的页面，已核实页面保持原结论。

    没有指定批次时，沿用最近一次采集（含被中断的批次）：只补读进度里
    仍待确认的页面，已核实页面不重读；若该批次已无待确认页面，
    返回现有进度。
    """
    if run_id is None:
        run_id = latest_run(path)
    if run_id is None:
        return collect(path, origin, editions or ('1',))
    run = get_run(path, run_id)
    if editions is None:
        editions = run['editions'] if run else ('1',)
    editions = tuple(str(edition) for edition in editions)
    for edition in editions:
        # 读目录才知道本期有哪些条目；它只决定“要核实哪些页”，不提供事实。
        _catalog_pages(origin, edition, run_id, path)
        report = progress_report(path, run_id=run_id)
        pending = [
            page['source_id']
            for page in report['pending']
            if str(page['edition']) == edition
        ]
        for source_id in pending:
            _verify(path, origin, source_id, edition, run_id)
    finish_run(path, run_id, state='finished')
    return run_id, progress_report(path, run_id=run_id)


def remaining(path, run_id=None):
    """续采前先看还剩哪些页待确认；没有指定批次时看最近一次采集。"""
    if run_id is None:
        run_id = latest_run(path)
    if run_id is None:
        return {'run_id': None, 'pending': [], 'verified': []}
    return progress_report(path, run_id=run_id)


__all__ = [
    'collect',
    'resume',
    'remaining',
]

"""解释目录与详情的业务身份，并判定发布覆盖范围。

这里区分三件事：

* 发布期（edition）：业务月份/发布批次，不同发布期的数据不能互相补齐；
* 发布版本（revision）：同一发布期内可以多次补发或修订；
* 覆盖范围（coverage）：某次发布实际包含哪些指标。

``facts`` 中不存在的指标表示“未公布/未知”，不是零；只有明确写入
``0`` 才是真正的零值。
"""

REQUIRED_FACTS = ('total', 'tourism_consulting', 'culture_sports_leisure')

METRIC_LABELS = {
    'total': '服务零售额总额',
    'tourism_consulting': '旅游咨询',
    'culture_sports_leisure': '文体休闲',
}


def interpret_catalog(row):
    """确认目录条目的稳定身份。"""
    if not row.get('id') or not row.get('name'):
        raise ValueError('目录身份不完整')
    return {'source_id': row['id'], 'group': row.get('group'), 'name': row['name']}


def interpret_detail(row):
    """确认详情页的发布期与发布版本身份。

    旧版来源没有单独的发布期字段，此时发布期沿用版本标识；新版来源两者
    必须分开，避免把“版本”误当成“月份”。
    """
    if not row.get('id') or row.get('revision') is None:
        raise ValueError('详情身份不完整')
    edition = row.get('edition')
    if edition is None:
        edition = row['revision']
    return {
        'source_id': row['id'],
        'group': row.get('group'),
        'edition': str(edition),
        'revision': str(row['revision']),
    }


def revision_key(revision):
    """让版本可以稳定比较；无法解析的版本仍可排序，但排在数字之后。"""
    if revision is None:
        return (0, '')
    text = str(revision)
    try:
        return (1, int(text))
    except ValueError:
        return (0, text)


def required_facts(row):
    """该条目需要覆盖的指标；未声明时使用统计公报的默认指标。

    未声明的来源也不能被当作“无需覆盖”，否则缺项会被误判为完整。
    """
    keys = row.get('required_coverage') or row.get('required_facts') or REQUIRED_FACTS
    return tuple(str(key) for key in keys)


def coverage(row):
    """该次发布明确声明的指标范围。"""
    return tuple(str(key) for key in (row.get('coverage') or ()))


def factual_values(row):
    """返回有明确取值的指标；``None`` 仍表示未知，不会被当成零。"""
    facts = row.get('facts') or {}
    return {str(key): value for key, value in facts.items() if value is not None}


def missing_facts(row):
    """缺少或没有明确取值的必需指标。"""
    values = factual_values(row)
    declared = set(coverage(row))
    return tuple(
        key for key in required_facts(row)
        if key not in values or key not in declared
    )


def is_complete(row):
    """只有当前发布状态有效且所有必需指标都有明确取值时才完整。"""
    if row.get('status', 'published') != 'published':
        return False
    return not missing_facts(row)

"""读取本地演练来源的公开资料，并演示连续采集的进度与当前事实。"""
import argparse
import json

from .capture import read_catalog, read_detail
from .pipeline import collect, resume
from .records import current_facts


def main(argv=None):
    parser = argparse.ArgumentParser(description='本地演练来源采集')
    parser.add_argument('--origin', default='http://127.0.0.1:8765')
    parser.add_argument('--edition', action='append', default=None,
                        help='发布期，可重复；默认 1')
    parser.add_argument('--records', default=None, help='收据数据库路径')
    parser.add_argument('--resume', action='store_true', help='续采未完成的批次')
    args = parser.parse_args(argv)
    editions = tuple(args.edition or ('1',))

    if args.records:
        if args.resume:
            run_id, report = resume(args.records, args.origin, editions)
        else:
            run_id, report = collect(args.records, args.origin, editions)
        print(json.dumps(
            {
                'run_id': run_id,
                'report': report,
                'current_facts': current_facts(args.records),
            },
            ensure_ascii=False,
            indent=2,
        ))
        return

    catalog = read_catalog(args.origin, editions[0])
    print(json.dumps(catalog, ensure_ascii=False, indent=2))
    first = read_detail(args.origin, catalog['items'][0]['id'], editions[0])
    print(json.dumps(first, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

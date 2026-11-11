import argparse
import json
import sys
from pathlib import Path
from .records import inspect_bundle
from .reclaim import ManualClock, ReclaimManager
from .storage import RevisionStore

def run_gc(args):
    manager=ReclaimManager(args.directory,ManualClock(getattr(args,'clock',0) or 0))
    if args.command=='gc-plan':
        plan_id,plan=manager.plan()
        return {'plan_id':plan_id,'roots_revision':plan['roots_revision'],
                'reclaimable':plan['reclaimable'],'reclaimable_bytes':plan['reclaimable_bytes']}
    if args.command=='gc-isolate':return manager.isolate(args.plan_id)
    if args.command=='gc-restore':return {'restored':manager.restore(args.shas)}
    if args.command=='gc-purge':return manager.purge()
    return manager.stats()

def main():
    parser=argparse.ArgumentParser(description='数据包内容检查与对象回收')
    commands=parser.add_subparsers(dest='command',required=True)
    p=commands.add_parser('inspect');p.add_argument('bundle',type=Path)
    p=commands.add_parser('catalog');p.add_argument('database',type=Path);p.add_argument('stream')
    p=commands.add_parser('gc-plan');p.add_argument('directory');p.add_argument('--clock',type=int,required=True)
    p=commands.add_parser('gc-isolate');p.add_argument('directory');p.add_argument('plan_id');p.add_argument('--clock',type=int,required=True)
    p=commands.add_parser('gc-restore');p.add_argument('directory');p.add_argument('shas',nargs='+');p.add_argument('--clock',type=int,required=True)
    p=commands.add_parser('gc-purge');p.add_argument('directory');p.add_argument('--clock',type=int,required=True)
    p=commands.add_parser('gc-stats');p.add_argument('directory')
    args=parser.parse_args()
    try:
        if args.command=='inspect':value=inspect_bundle(args.bundle)
        elif args.command=='catalog':value=RevisionStore(args.database).latest(args.stream)
        else:value=run_gc(args)
        print(json.dumps(value,ensure_ascii=False,indent=2));return 0
    except (ValueError,KeyError,OSError):
        print('输入资料或目录无效，请检查路径及字段说明',file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())

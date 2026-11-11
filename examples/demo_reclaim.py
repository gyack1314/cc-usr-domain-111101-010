"""可恢复对象回收演示：共享对象保留、新pin使旧计划失败、隔离恢复与中断续接。

运行：python examples/demo_reclaim.py [工作目录]
默认工作目录 output/reclaim-demo（已排除出版本控制）；若已有演示数据会先清空重建。
时间线取自 examples/logical-clock.json，全部使用合成对象，离线运行。
"""
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from datapack.reclaim import (BreakpointHooks, InjectedInterrupt, ManualClock,
                              ReclaimManager, StalePlan, load_sample_roots)
from datapack.seed import initialize


def show(title, value):
    print('\n== ' + title)
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main():
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / 'output' / 'reclaim-demo'
    if (directory / 'seed.json').exists():
        shutil.rmtree(directory)
    timeline = json.loads((REPO / 'examples' / 'logical-clock.json').read_text(encoding='utf-8'))
    names = initialize(directory)
    clock = ManualClock(timeline['plan_at'])
    manager = ReclaimManager(directory, clock)
    manager.registry.put(0, load_sample_roots(REPO / 'samples', names))
    published = {sha: manager.store.path(sha).read_bytes() for sha in manager.store.addresses()}

    # 1) 计划：证明孤立对象不再被有效根引用；共享对象被快照与读者pin保护
    plan_id, plan = manager.plan()
    reclaimable = {item['sha'] for item in plan['reclaimable']}
    assert reclaimable == {names['orphan'], names['orphan-b']}, reclaimable
    assert names['shared'] in plan['reachable']
    show('计划：可回收对象、字节数与理由', {
        'plan_id': plan_id,
        'roots_revision': plan['roots_revision'],
        'reclaimable_bytes': plan['reclaimable_bytes'],
        'reclaimable': plan['reclaimable'],
    })

    # 2) 计划之后新增读者pin：根集合版本推进，旧计划围栏失效，拒绝先删后查
    clock.set(timeline['pin_renew_at'])
    document = manager.registry.document()
    document['pins'].append({'id': 'reader-2', 'object': names['orphan'], 'expires': 300})
    manager.registry.put(manager.registry.revision(), document)
    try:
        manager.isolate(plan_id)
        raise AssertionError('旧计划不应生效')
    except StalePlan as error:
        show('新pin使旧计划失效', {'error': str(error)})

    # 3) 重建计划并隔离：注入中断后按实际位置续接，不重复计量
    plan_id, plan = manager.plan()
    assert {item['sha'] for item in plan['reclaimable']} == {names['orphan-b']}
    hooks = BreakpointHooks()
    hooks.fail_once('isolate.after_move')
    try:
        ReclaimManager(directory, clock, hooks=hooks).isolate(plan_id)
        raise AssertionError('应注入中断')
    except InjectedInterrupt as error:
        show('隔离中断现场（对象已移动、账本未写）', {
            'error': str(error), 'trash': manager.stats()['trash'],
            'ledger': manager.stats()['ledger']})
    result = manager.isolate(plan_id)
    assert result['already_trashed'] == [names['orphan-b']], result
    show('隔离中断后续接完成', result)

    # 4) 保留期内恢复到原内容地址，并从活跃快照实际读取内容验证
    clock.set(timeline['restore_at'])
    assert manager.restore([names['orphan-b']]) == [names['orphan-b']]
    snapshot_a = manager.store.read(names['manifest-a'])
    values = [manager.store.read(ref) for ref in snapshot_a['refs']]
    assert manager.store.read(names['orphan-b']) == {'kind': 'data', 'values': [10, 11]}
    show('恢复后从活跃快照实际读取', {'manifest-a refs': snapshot_a['refs'], 'data': values})

    # 5) 恢复后的对象再次失去引用：重新计划并隔离（新一轮围栏）
    plan_id, plan = manager.plan()
    assert {item['sha'] for item in plan['reclaimable']} == {names['orphan-b']}
    manager.isolate(plan_id)

    # 6) 宽限期内purge保留；宽限期后purge重新确认无引用才清除
    clock.set(timeline['purge_at'])
    result = manager.purge()
    assert result['kept'] == [names['orphan-b']], result
    show('宽限期内purge保留', result)
    clock.set(clock.now + timeline['trash_grace'])
    result = manager.purge()
    assert result['purged'] == [names['orphan-b']], result
    show('宽限期后purge完成', result)

    # 7) 共享对象与已发布清单内容全程未被修改
    for sha, data in published.items():
        if sha == names['orphan-b']:
            continue
        assert manager.store.path(sha).read_bytes() == data
    assert manager.store.read(names['shared']) == {'kind': 'data', 'values': [1, 2, 3]}
    show('最终字节统计', manager.stats())
    print('\n演示完成：共享对象保留、新pin使旧计划失败、隔离恢复与隔离中断后续接均已验证。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

import hashlib
import tempfile
import unittest
from pathlib import Path

from datapack.domain import manifest
from datapack.reclaim import (BreakpointHooks, CorruptionError, InjectedInterrupt,
                              ManualClock, ReclaimConflict, ReclaimManager, StalePlan)
from datapack.storage import atomic_write, canonical

SHARED = {'kind': 'data', 'values': [1, 2, 3]}
ONLY_A = {'kind': 'data', 'values': [4, 5]}
ORPHAN = {'kind': 'data', 'values': [8, 9]}
ORPHAN_B = {'kind': 'data', 'values': [10, 11]}


class ReclaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = ManualClock(100)
        self.manager = ReclaimManager(Path(self.tmp.name) / 'store', self.clock)
        self.store = self.manager.store
        self.shared = self.store.put(SHARED)
        self.only_a = self.store.put(ONLY_A)
        self.orphan = self.store.put(ORPHAN)
        self.orphan_b = self.store.put(ORPHAN_B)
        self.manifest_a = self.store.put(manifest([self.shared, self.only_a]))
        self.manifest_b = self.store.put(manifest([self.shared]))

    def set_roots(self, snapshots=None, pins=None, grace=30):
        document = {
            'snapshots': snapshots if snapshots is not None else [
                {'id': 'snap-b', 'object': self.manifest_b, 'expires': 0, 'hold': True}],
            'pins': pins if pins is not None else [
                {'id': 'reader-1', 'object': self.manifest_a, 'expires': 150}],
            'retention': {'trash_grace': grace},
        }
        registry = self.manager.registry
        return registry.put(registry.revision(), document)

    def plan_and_isolate(self, grace=30):
        self.set_roots(grace=grace)
        plan_id, plan = self.manager.plan()
        self.manager.isolate(plan_id)
        return plan_id, plan

    def test_plan_records_fence_bytes_and_reasons(self):
        self.set_roots()
        plan_id, plan = self.manager.plan()
        self.assertEqual(plan['roots_revision'], 1)
        self.assertEqual(plan['trash_grace'], 30)
        reclaimable = {item['sha']: item for item in plan['reclaimable']}
        self.assertEqual(set(reclaimable), {self.orphan, self.orphan_b})
        self.assertEqual(plan['reclaimable_bytes'],
                         len(canonical(ORPHAN)) + len(canonical(ORPHAN_B)))
        self.assertTrue(all(item['reason'] for item in plan['reclaimable']))
        # 共享对象被两个清单引用，可达且绝不进入可回收清单
        self.assertIn(self.shared, plan['reachable'])
        self.assertNotIn(self.shared, reclaimable)
        self.assertTrue((self.manager.dir / 'gc' / 'plans' / (plan_id + '.json')).is_file())

    def test_isolate_quarantines_and_stats_match(self):
        plan_id, plan = self.plan_and_isolate()
        # 活跃快照内容仍可从原地址实际读取
        self.assertEqual(self.store.read(self.manifest_a)['refs'], [self.shared, self.only_a])
        self.assertEqual(self.store.read(self.shared), SHARED)
        self.assertFalse(self.store.path(self.orphan).exists())
        self.assertTrue(self.manager.trash_path(self.orphan).is_file())
        stats = self.manager.stats()
        self.assertEqual(stats['trash']['bytes'], plan['reclaimable_bytes'])
        self.assertEqual(stats['trash']['objects'], 2)
        self.assertEqual(stats['live']['objects'], 4)
        self.assertEqual(stats['ledger']['trashed']['bytes'], plan['reclaimable_bytes'])
        # 重复执行识别实际位置，不重复计量
        again = self.manager.isolate(plan_id)
        self.assertEqual(set(again['already_trashed']), {self.orphan, self.orphan_b})
        self.assertEqual(again['moved'], [])
        self.assertEqual(self.manager.stats()['trash']['bytes'], plan['reclaimable_bytes'])

    def test_stale_plan_on_root_changes(self):
        self.set_roots()
        registry = self.manager.registry
        plan_id, _ = self.manager.plan()
        # 计划之后新增读者pin
        document = registry.document()
        document['pins'].append({'id': 'reader-2', 'object': self.orphan, 'expires': 300})
        registry.put(registry.revision(), document)
        with self.assertRaises(StalePlan):
            self.manager.isolate(plan_id)
        # 读者续租（延长过期时间）同样使旧计划失效
        plan_id, _ = self.manager.plan()
        document = registry.document()
        document['pins'][1]['expires'] = 400
        registry.put(registry.revision(), document)
        with self.assertRaises(StalePlan):
            self.manager.isolate(plan_id)
        # 新增快照与保留变更也一样
        plan_id, _ = self.manager.plan()
        document = registry.document()
        document['snapshots'].append(
            {'id': 'snap-c', 'object': self.manifest_a, 'expires': 500, 'hold': False})
        registry.put(registry.revision(), document)
        with self.assertRaises(StalePlan):
            self.manager.isolate(plan_id)
        # 重建计划后围栏一致；被新pin引用的orphan保留，orphan-b可回收
        plan_id, plan = self.manager.plan()
        self.assertEqual({item['sha'] for item in plan['reclaimable']}, {self.orphan_b})
        self.manager.isolate(plan_id)
        self.assertEqual(self.store.read(self.orphan), ORPHAN)

    def test_restore_returns_object_to_live(self):
        self.plan_and_isolate()
        self.assertEqual(self.manager.restore([self.orphan]), [self.orphan])
        # 从原内容地址实际读取，不只看账本
        self.assertEqual(self.store.read(self.orphan), ORPHAN)
        self.assertFalse(self.manager.trash_path(self.orphan).exists())
        self.assertEqual(self.manager.ledger.entry(self.orphan)['state'], 'restored')
        # 幂等：已在原位再次恢复安全
        self.assertEqual(self.manager.restore([self.orphan]), [self.orphan])
        stats = self.manager.stats()
        self.assertEqual(stats['trash']['objects'], 1)
        self.assertEqual(stats['ledger']['restored']['bytes'], len(canonical(ORPHAN)))

    def test_isolate_interrupt_resume(self):
        self.set_roots()
        plan_id, plan = self.manager.plan()
        hooks = BreakpointHooks()
        hooks.fail_once('isolate.after_move')
        crashing = ReclaimManager(self.manager.dir, self.clock, hooks=hooks)
        with self.assertRaises(InjectedInterrupt):
            crashing.isolate(plan_id)
        # 中断现场：一个对象已移动未记账，另一个仍在live
        trashed = [sha for sha in (self.orphan, self.orphan_b)
                   if self.manager.trash_path(sha).is_file()]
        self.assertEqual(len(trashed), 1)
        self.assertIsNone(self.manager.ledger.entry(trashed[0]))
        # 续接：识别实际位置补账，不重复移动、不重复计量
        result = self.manager.isolate(plan_id)
        self.assertEqual(set(result['already_trashed']), set(trashed))
        self.assertEqual(len(result['moved']), 1)
        stats = self.manager.stats()
        self.assertEqual(stats['trash']['bytes'], plan['reclaimable_bytes'])
        self.assertEqual(stats['ledger']['trashed']['objects'], 2)

    def test_restore_interrupt_resume(self):
        self.plan_and_isolate()
        hooks = BreakpointHooks()
        hooks.fail_once('restore.after_move')
        crashing = ReclaimManager(self.manager.dir, self.clock, hooks=hooks)
        with self.assertRaises(InjectedInterrupt):
            crashing.restore([self.orphan])
        # 文件已回live，账本仍记trashed；续接只补账
        self.assertEqual(self.store.read(self.orphan), ORPHAN)
        self.assertEqual(self.manager.ledger.entry(self.orphan)['state'], 'trashed')
        self.assertEqual(self.manager.restore([self.orphan]), [self.orphan])
        self.assertEqual(self.manager.ledger.entry(self.orphan)['state'], 'restored')

    def test_purge_interrupt_resume(self):
        plan_id, plan = self.plan_and_isolate(grace=0)
        hooks = BreakpointHooks()
        hooks.fail_once('purge.after_delete')
        crashing = ReclaimManager(self.manager.dir, self.clock, hooks=hooks)
        with self.assertRaises(InjectedInterrupt):
            crashing.purge()
        # 一个文件已删但账本仍记trashed
        gone = [sha for sha in (self.orphan, self.orphan_b)
                if not self.manager.trash_path(sha).is_file()]
        self.assertEqual(len(gone), 1)
        self.assertEqual(self.manager.ledger.entry(gone[0])['state'], 'trashed')
        # 续接：识别文件实际不存在，补账不重复计量
        result = self.manager.purge()
        self.assertEqual(set(result['reconciled']), set(gone))
        stats = self.manager.stats()
        self.assertEqual(stats['reclaimed_bytes'], plan['reclaimable_bytes'])
        self.assertEqual(stats['trash']['objects'], 0)

    def test_purge_restores_re_referenced_objects(self):
        self.plan_and_isolate()
        # 隔离期间新增根引用已隔离对象：purge必须恢复而非清除
        registry = self.manager.registry
        document = registry.document()
        document['snapshots'].append(
            {'id': 'snap-c', 'object': self.orphan, 'expires': 500, 'hold': False})
        registry.put(registry.revision(), document)
        self.clock.set(200)
        result = self.manager.purge()
        self.assertEqual(result['restored'], [self.orphan])
        self.assertEqual(result['purged'], [self.orphan_b])
        self.assertEqual(self.store.read(self.orphan), ORPHAN)
        self.assertFalse(self.manager.trash_path(self.orphan_b).exists())
        self.assertEqual(self.manager.ledger.entry(self.orphan)['state'], 'restored')
        self.assertEqual(self.manager.ledger.entry(self.orphan_b)['state'], 'purged')

    def test_purge_keeps_within_grace_and_deletes_after(self):
        self.plan_and_isolate(grace=30)  # 100秒时隔离，130秒起可清除
        self.clock.set(120)
        result = self.manager.purge()
        self.assertEqual(set(result['kept']), {self.orphan, self.orphan_b})
        self.assertEqual(result['purged'], [])
        self.assertTrue(self.manager.trash_path(self.orphan).is_file())
        self.clock.set(130)
        result = self.manager.purge()
        self.assertEqual(set(result['purged']), {self.orphan, self.orphan_b})
        self.assertEqual(result['purged_bytes'],
                         len(canonical(ORPHAN)) + len(canonical(ORPHAN_B)))
        self.assertEqual(self.manager.stats()['live']['objects'], 4)

    def test_missing_and_digest_corruption_block_plan(self):
        self.set_roots()
        # 被引用对象缺失：阻断并指出损坏位置，不能当成无引用
        self.store.path(self.only_a).unlink()
        with self.assertRaises(CorruptionError) as ctx:
            self.manager.plan()
        self.assertIn(self.only_a, str(ctx.exception))
        self.store.put(ONLY_A)
        # 摘要损坏：即使对象本来无引用，也不能宣称回收安全
        self.store.path(self.orphan).write_bytes(b'garbage')
        with self.assertRaises(CorruptionError) as ctx:
            self.manager.plan()
        self.assertIn(self.orphan, str(ctx.exception))
        self.assertIn('摘要', str(ctx.exception))

    def test_trash_corruption_blocks_purge(self):
        self.plan_and_isolate(grace=0)
        # 摘要自洽但JSON无法解析的隔离对象
        bad = b'{"kind": "manifest", "refs": [}'
        bad_sha = hashlib.sha256(bad).hexdigest()
        bad_path = self.manager.trash_path(bad_sha)
        atomic_write(bad_path, bad)
        with self.assertRaises(CorruptionError) as ctx:
            self.manager.purge()
        self.assertIn(bad_sha, str(ctx.exception))
        self.assertIn('解析', str(ctx.exception))
        bad_path.unlink()
        # kind未知同样阻断
        weird = canonical({'kind': 'weird'})
        weird_sha = hashlib.sha256(weird).hexdigest()
        weird_path = self.manager.trash_path(weird_sha)
        atomic_write(weird_path, weird)
        with self.assertRaises(CorruptionError):
            self.manager.purge()
        weird_path.unlink()
        # 修复后本轮可继续
        result = self.manager.purge()
        self.assertEqual(set(result['purged']), {self.orphan, self.orphan_b})

    def test_deep_manifest_and_duplicate_refs(self):
        # 深层清单链：迭代遍历不递归溢出
        head = self.store.put({'kind': 'manifest', 'refs': [self.shared], 'note': 'chain'})
        for _ in range(1500):
            head = self.store.put(manifest([head]))
        # 重复引用与菱形引用：只展开一次、只计量一次
        dup_data = canonical({'kind': 'manifest', 'refs': [self.only_a, self.only_a]})
        dup_sha = hashlib.sha256(dup_data).hexdigest()
        atomic_write(self.store.path(dup_sha), dup_data)
        diamond_x = self.store.put({'kind': 'data', 'values': [42]})
        branch1 = self.store.put({'kind': 'manifest', 'refs': [diamond_x], 'note': 'a'})
        branch2 = self.store.put({'kind': 'manifest', 'refs': [diamond_x], 'note': 'b'})
        top = self.store.put(manifest([branch1, branch2]))
        self.set_roots(snapshots=[
            {'id': 'deep', 'object': head, 'expires': 0, 'hold': True},
            {'id': 'dup', 'object': dup_sha, 'expires': 0, 'hold': True},
            {'id': 'diamond', 'object': top, 'expires': 0, 'hold': True},
        ], pins=[])
        _, plan = self.manager.plan()
        self.assertEqual({item['sha'] for item in plan['reclaimable']},
                         {self.orphan, self.orphan_b, self.manifest_a, self.manifest_b})
        self.assertIn(head, plan['reachable'])
        self.assertIn(self.shared, plan['reachable'])
        self.assertIn(self.only_a, plan['reachable'])
        self.assertIn(diamond_x, plan['reachable'])
        # 字节统计与磁盘实际一致，每个对象只算一次
        expect = sum(len(self.store.path(sha).read_bytes()) for sha in self.store.addresses())
        self.assertEqual(self.manager.stats()['live']['bytes'], expect)

    def test_same_address_different_content_never_overwritten(self):
        self.set_roots()
        plan_id, _ = self.manager.plan()
        # 隔离区被预先放入同地址不同内容：拒绝覆盖并保留原文件
        trash = self.manager.trash_path(self.orphan)
        atomic_write(trash, b'intruder')
        with self.assertRaises(ReclaimConflict):
            self.manager.isolate(plan_id)
        self.assertEqual(trash.read_bytes(), b'intruder')
        trash.unlink()
        self.manager.isolate(plan_id)
        # 恢复时live出现同地址不同内容：同样拒绝覆盖
        self.store.path(self.orphan).write_bytes(b'intruder')
        with self.assertRaises(ReclaimConflict):
            self.manager.restore([self.orphan])
        self.assertEqual(self.store.path(self.orphan).read_bytes(), b'intruder')
        self.assertTrue(trash.is_file())

    def test_published_manifests_never_modified(self):
        self.set_roots()
        before = {sha: self.store.path(sha).read_bytes() for sha in self.store.addresses()}
        plan_id, _ = self.manager.plan()
        self.manager.isolate(plan_id)
        self.manager.restore([self.orphan])
        self.clock.set(200)
        self.manager.purge()
        for sha, data in before.items():
            if sha == self.orphan_b:
                continue  # 唯一被清除的对象
            self.assertEqual(self.store.path(sha).read_bytes(), data)
        self.assertEqual(self.store.read(self.manifest_a)['refs'], [self.shared, self.only_a])

    def test_expiry_boundary_and_hold(self):
        # expires等于当前时刻即过期；hold不看expires
        self.set_roots(snapshots=[
            {'id': 'expired', 'object': self.manifest_a, 'expires': 100, 'hold': False},
            {'id': 'held', 'object': self.manifest_b, 'expires': 0, 'hold': True},
        ], pins=[{'id': 'boundary-reader', 'object': self.orphan, 'expires': 100}])
        _, plan = self.manager.plan()
        reclaimable = {item['sha'] for item in plan['reclaimable']}
        self.assertIn(self.only_a, reclaimable)
        self.assertIn(self.manifest_a, reclaimable)
        self.assertIn(self.orphan, reclaimable)
        self.assertNotIn(self.shared, reclaimable)
        self.assertNotIn(self.manifest_b, reclaimable)


if __name__ == '__main__':
    unittest.main()

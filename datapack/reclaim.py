"""可恢复对象回收：先证明无引用，再隔离，过了保留期才清除。

plan 从有效根出发迭代遍历并全量核验，证明对象不再被任何有效根引用后，
生成记录根集合版本、可回收对象、字节数及理由的计划文件；isolate 核对计划
围栏，把对象移入 trash 并写持久回收账，中断后按实际位置安全续接；restore
在保留期内把对象恢复到原内容地址；purge 在宽限期后重新确认根可达性，重新
被引用的对象恢复而非删除。缺对象、摘要不符或清单解析失败都阻断本轮并指出
损坏位置，失败的遍历不会被当成无引用。已发布对象内容全程不被修改。
"""
import contextlib
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from .domain import ContentStore
from .storage import Conflict, RevisionStore, atomic_write, canonical, fingerprint, read_json

SHA_RE = re.compile('[0-9a-f]{64}')
ROOT_STREAM = 'roots'


class ReclaimError(ValueError):
    """回收操作的基础错误。"""


class CorruptionError(ReclaimError):
    """缺对象、摘要不符或清单解析失败；阻断本轮并指出损坏位置。"""

    def __init__(self, problem, sha=None, where=None):
        self.problem = problem
        self.sha = sha
        self.where = where
        message = problem if sha is None else '%s：%s（%s空间）' % (problem, sha, where)
        super().__init__(message)


class StalePlan(ReclaimError):
    """计划围栏与当前根集合不一致，旧计划失效，必须重建。"""


class ReclaimConflict(ReclaimError):
    """同一内容地址出现不同内容，拒绝覆盖。"""


class InjectedInterrupt(ReclaimError):
    """注入的中断，用于演练崩溃后的续接。"""


class ManualClock:
    """可注入的逻辑时钟，单位秒，由调用方推进。"""

    def __init__(self, now=0):
        self.now = int(now)

    def __call__(self):
        return self.now

    def set(self, now):
        self.now = int(now)

    def advance(self, delta):
        self.now += int(delta)


class BreakpointHooks:
    """中断注入点：在关键持久化步骤之间触发，用于演示与测试崩溃续接。"""

    POINTS = (
        'isolate.after_move',
        'isolate.after_entry',
        'restore.after_move',
        'restore.after_entry',
        'purge.after_restore',
        'purge.after_delete',
        'purge.after_entry',
    )

    def __init__(self):
        self._handlers = {}

    def on(self, point, handler):
        if point not in self.POINTS:
            raise ValueError('未知中断点：' + point)
        self._handlers.setdefault(point, []).append(handler)

    def fail_once(self, point):
        def handler(_info):
            self._handlers[point].remove(handler)
            raise InjectedInterrupt('注入中断：' + point)
        self.on(point, handler)

    def fire(self, point, **info):
        for handler in list(self._handlers.get(point, [])):
            handler(info)


def _empty_roots():
    return {'snapshots': [], 'pins': [], 'retention': {'trash_grace': 0}}


def _validate_roots(document):
    if not isinstance(document, dict):
        raise ValueError('根集合文档必须是对象')
    snapshots = document.get('snapshots')
    pins = document.get('pins')
    retention = document.get('retention')
    if not isinstance(snapshots, list) or not isinstance(pins, list) or not isinstance(retention, dict):
        raise ValueError('根集合文档需要snapshots、pins与retention')
    for snap in snapshots:
        if (not isinstance(snap, dict) or not isinstance(snap.get('id'), str) or not snap['id']
                or not isinstance(snap.get('object'), str) or SHA_RE.fullmatch(snap['object']) is None
                or not isinstance(snap.get('expires'), int) or isinstance(snap.get('expires'), bool)
                or not isinstance(snap.get('hold'), bool)):
            raise ValueError('快照根记录无效：' + repr(snap))
    for pin in pins:
        if (not isinstance(pin, dict) or not isinstance(pin.get('id'), str) or not pin['id']
                or not isinstance(pin.get('object'), str) or SHA_RE.fullmatch(pin['object']) is None
                or not isinstance(pin.get('expires'), int) or isinstance(pin.get('expires'), bool)):
            raise ValueError('读者pin记录无效：' + repr(pin))
    grace = retention.get('trash_grace')
    if not isinstance(grace, int) or isinstance(grace, bool) or grace < 0:
        raise ValueError('保留期trash_grace必须是非负整数秒')


def valid_roots(document, now):
    """按调用方逻辑时钟选出有效根：未过期快照、人工保留项与未过期读者pin。

    expires等于当前时刻即已过期；hold为真的人工保留项不看expires。
    返回(有效根列表, 隔离宽限期秒)。
    """
    roots = []
    for snap in document['snapshots']:
        if snap['hold']:
            roots.append({'id': snap['id'], 'kind': 'snapshot-hold', 'object': snap['object']})
        elif snap['expires'] > now:
            roots.append({'id': snap['id'], 'kind': 'snapshot', 'object': snap['object']})
    for pin in document['pins']:
        if pin['expires'] > now:
            roots.append({'id': pin['id'], 'kind': 'pin', 'object': pin['object']})
    return roots, document['retention']['trash_grace']


class RootRegistry:
    """根集合登记处：单流单调版本作为计划围栏，任何根变化都推进版本。"""

    def __init__(self, path):
        self._store = RevisionStore(path)

    def revision(self):
        latest = self._store.latest(ROOT_STREAM)
        return 0 if latest is None else latest['revision']

    def document(self):
        latest = self._store.latest(ROOT_STREAM)
        return _empty_roots() if latest is None else latest['value']

    def put(self, expected, document):
        _validate_roots(document)
        return self._store.put(ROOT_STREAM, expected, document)

    def update(self, fn):
        """读-改-写根集合，版本冲突时重试；返回新版本号。"""
        while True:
            revision = self.revision()
            document = fn(self.document())
            _validate_roots(document)
            try:
                return self._store.put(ROOT_STREAM, revision, document)
            except Conflict:
                continue


class GCLedger:
    """持久回收账：计划状态与每个隔离对象的字节数、位置与状态。

    对象地址是主键，重复隔离或续接只更新同一行，不重复计量。
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS gc_plans ('
                         'plan_id TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at INTEGER NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS gc_entries ('
                         'sha TEXT PRIMARY KEY, plan_id TEXT NOT NULL, size INTEGER NOT NULL, '
                         'trashed_at INTEGER NOT NULL, grace INTEGER NOT NULL, '
                         'state TEXT NOT NULL, note TEXT NOT NULL DEFAULT \'\')')

    @contextlib.contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def set_plan_status(self, plan_id, status, now):
        with self._connection() as conn:
            conn.execute('INSERT OR REPLACE INTO gc_plans VALUES(?,?,?)', (plan_id, status, int(now)))

    def plan_status(self, plan_id):
        with self._connection() as conn:
            row = conn.execute('SELECT status FROM gc_plans WHERE plan_id=?', (plan_id,)).fetchone()
        return None if row is None else row[0]

    def upsert_entry(self, sha, plan_id, size, trashed_at, grace, state, note=''):
        with self._connection() as conn:
            conn.execute('INSERT OR REPLACE INTO gc_entries VALUES(?,?,?,?,?,?,?)',
                         (sha, plan_id, int(size), int(trashed_at), int(grace), state, note))

    def set_entry_state(self, sha, state, note):
        with self._connection() as conn:
            cursor = conn.execute('UPDATE gc_entries SET state=?, note=? WHERE sha=?', (state, note, sha))
            if cursor.rowcount == 0:
                raise KeyError('回收账中不存在对象：' + sha)

    def entry(self, sha):
        with self._connection() as conn:
            row = conn.execute('SELECT sha,plan_id,size,trashed_at,grace,state,note '
                               'FROM gc_entries WHERE sha=?', (sha,)).fetchone()
        return None if row is None else dict(
            zip(('sha', 'plan_id', 'size', 'trashed_at', 'grace', 'state', 'note'), row))

    def entries(self, state=None):
        sql = 'SELECT sha,plan_id,size,trashed_at,grace,state,note FROM gc_entries'
        args = ()
        if state is not None:
            sql += ' WHERE state=?'
            args = (state,)
        with self._connection() as conn:
            rows = conn.execute(sql + ' ORDER BY sha', args).fetchall()
        return [dict(zip(('sha', 'plan_id', 'size', 'trashed_at', 'grace', 'state', 'note'), row))
                for row in rows]


def _strict_loads(data, sha, where):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError('JSON键重复：' + key)
            out[key] = value
        return out

    def nonfinite(value):
        raise ValueError('JSON数值不能为' + value)

    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, UnicodeDecodeError):
        raise CorruptionError('对象JSON解析失败', sha, where) from None


def _manifest_refs(value, sha, where):
    if not isinstance(value, dict) or not isinstance(value.get('kind'), str):
        raise CorruptionError('对象缺少kind字段', sha, where)
    kind = value['kind']
    if kind == 'data':
        return []
    if kind != 'manifest':
        raise CorruptionError('对象kind未知：' + kind, sha, where)
    refs = value.get('refs')
    if not isinstance(refs, list) or any(not isinstance(ref, str) or SHA_RE.fullmatch(ref) is None
                                         for ref in refs):
        raise CorruptionError('清单refs无效', sha, where)
    return refs


class ReclaimManager:
    """计划、隔离、恢复与清除的编排；只移动对象位置，不修改任何已发布内容。"""

    def __init__(self, directory, clock, hooks=None):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.store = ContentStore(self.dir / 'objects')
        self.trash_dir = self.dir / 'trash'
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        gc_dir = self.dir / 'gc'
        (gc_dir / 'plans').mkdir(parents=True, exist_ok=True)
        self.registry = RootRegistry(gc_dir / 'roots.sqlite3')
        self.ledger = GCLedger(gc_dir / 'ledger.sqlite3')
        self.clock = clock
        self.hooks = hooks if hooks is not None else BreakpointHooks()

    # ---- 对象定位与核验 ----

    def trash_path(self, sha):
        if not isinstance(sha, str) or SHA_RE.fullmatch(sha) is None:
            raise ValueError('对象地址无效：' + repr(sha))
        return self.trash_dir / sha[:2] / sha[2:]

    def _locate(self, sha, spaces):
        for where in spaces:
            path = self.store.path(sha) if where == 'live' else self.trash_path(sha)
            if path.is_file():
                return where
        return None

    def _read_object(self, sha, where):
        """读取并核验对象：存在性、内容摘要、JSON与清单结构；失败指出损坏位置。"""
        path = self.store.path(sha) if where == 'live' else self.trash_path(sha)
        try:
            data = path.read_bytes()
        except OSError:
            raise CorruptionError('对象缺失', sha, where) from None
        if hashlib.sha256(data).hexdigest() != sha:
            raise CorruptionError('对象摘要损坏', sha, where)
        value = _strict_loads(data, sha, where)
        return value, _manifest_refs(value, sha, where), len(data)

    def _verify_space(self, where):
        """核验空间内全部对象的摘要与结构，返回{地址: 字节数}；任何损坏阻断本轮。"""
        base = self.store.root if where == 'live' else self.trash_dir
        sizes = {}
        for path in sorted(base.glob('*/*')):
            name = path.parent.name + path.name
            if not path.is_file() or SHA_RE.fullmatch(name) is None:
                continue
            _, _, size = self._read_object(name, where)
            sizes[name] = size
        return sizes

    def _traverse(self, addresses, spaces):
        """从根地址迭代遍历：显式栈不递归，重复引用只展开一次，不重复计量。"""
        seen = set()
        stack = list(addresses)
        parent = {sha: None for sha in addresses}
        while stack:
            sha = stack.pop()
            if sha in seen:
                continue
            where = self._locate(sha, spaces)
            if where is None:
                source = parent.get(sha)
                problem = '根对象缺失' if source is None else '被%s引用的对象缺失' % source
                raise CorruptionError(problem, sha, '/'.join(spaces))
            seen.add(sha)
            _, refs, _ = self._read_object(sha, where)
            for ref in refs:
                if ref not in parent:
                    parent[ref] = sha
                stack.append(ref)
        return seen

    # ---- 计划 ----

    def plan(self):
        """证明并生成回收计划：全量核验加有效根遍历，任何损坏阻断本轮。

        计划记录根集合版本、有效根、可回收对象、字节数及理由，文件内容寻址持久化。
        """
        now = self.clock()
        document = self.registry.document()
        roots, grace = valid_roots(document, now)
        live_sizes = self._verify_space('live')
        reachable = self._traverse([root['object'] for root in roots], spaces=('live', 'trash'))
        reclaimable = [
            {'sha': sha, 'bytes': live_sizes[sha],
             'reason': '逻辑时钟%d时不被任何有效根引用' % now}
            for sha in sorted(live_sizes) if sha not in reachable
        ]
        plan = {
            'kind': 'reclaim-plan',
            'created_at': now,
            'roots_revision': self.registry.revision(),
            'roots_digest': fingerprint(document),
            'trash_grace': grace,
            'live_roots': roots,
            'reachable': sorted(reachable),
            'reclaimable': reclaimable,
            'reclaimable_bytes': sum(item['bytes'] for item in reclaimable),
        }
        plan_id = fingerprint(plan)
        atomic_write(self.dir / 'gc' / 'plans' / (plan_id + '.json'), canonical(plan))
        self.ledger.set_plan_status(plan_id, 'planned', now)
        return plan_id, plan

    def _load_plan(self, plan_id):
        if not isinstance(plan_id, str) or SHA_RE.fullmatch(plan_id) is None:
            raise ValueError('计划编号无效：' + repr(plan_id))
        path = self.dir / 'gc' / 'plans' / (plan_id + '.json')
        try:
            data = path.read_bytes()
        except OSError:
            raise ValueError('计划不存在：' + plan_id) from None
        if hashlib.sha256(data).hexdigest() != plan_id:
            raise CorruptionError('计划文件摘要不符', plan_id, 'gc/plans')
        plan = _strict_loads(data, plan_id, 'gc/plans')
        if not isinstance(plan, dict) or plan.get('kind') != 'reclaim-plan' \
                or not isinstance(plan.get('reclaimable'), list) \
                or not isinstance(plan.get('roots_revision'), int):
            raise CorruptionError('计划文件结构无效', plan_id, 'gc/plans')
        return plan

    def _check_fence(self, plan):
        """核对计划围栏：根集合版本或内容变化后，旧计划失效，不允许先删再发现。"""
        revision = self.registry.revision()
        if revision != plan['roots_revision']:
            raise StalePlan('根集合版本已变化：计划基于版本%d，当前版本%d；旧计划失效，请重建'
                            % (plan['roots_revision'], revision))
        if fingerprint(self.registry.document()) != plan['roots_digest']:
            raise StalePlan('根集合内容已变化；旧计划失效，请重建')

    # ---- 隔离 ----

    def isolate(self, plan_id):
        """执行计划：核对围栏后把可回收对象移入trash并写账；中断后可重复调用续接。"""
        plan = self._load_plan(plan_id)
        self._check_fence(plan)
        now = self.clock()
        self.ledger.set_plan_status(plan_id, 'isolating', now)
        moved, already, skipped = [], [], []
        for item in plan['reclaimable']:
            sha = item['sha']
            live = self.store.path(sha)
            trash = self.trash_path(sha)
            entry = self.ledger.entry(sha)
            if live.is_file():
                data = live.read_bytes()
                if hashlib.sha256(data).hexdigest() != sha:
                    raise CorruptionError('对象摘要损坏，阻断隔离', sha, 'live')
                if trash.exists():
                    if trash.read_bytes() != data:
                        raise ReclaimConflict('同一地址已存在不同内容，拒绝覆盖：' + sha)
                    live.unlink()
                else:
                    trash.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(live, trash)
                self.hooks.fire('isolate.after_move', sha=sha)
                self.ledger.upsert_entry(sha, plan_id, len(data), now, plan['trash_grace'], 'trashed')
                self.hooks.fire('isolate.after_entry', sha=sha)
                moved.append(sha)
            elif trash.is_file():
                data = trash.read_bytes()
                if hashlib.sha256(data).hexdigest() != sha:
                    raise CorruptionError('对象摘要损坏，阻断隔离', sha, 'trash')
                if entry is None or entry['state'] != 'trashed':
                    self.ledger.upsert_entry(sha, plan_id, len(data), now,
                                             plan['trash_grace'], 'trashed', note='中断续接补账')
                already.append(sha)
            elif entry is not None and entry['state'] == 'restored':
                skipped.append(sha)
            else:
                raise CorruptionError('计划对象缺失，阻断隔离', sha, 'live/trash')
        self.ledger.set_plan_status(plan_id, 'isolated', now)
        return {'plan_id': plan_id, 'moved': moved, 'already_trashed': already,
                'skipped_restored': skipped}

    # ---- 恢复 ----

    def restore(self, shas):
        """把隔离中的对象恢复到原内容地址；已在原位的重复调用安全。"""
        now = self.clock()
        restored = []
        for sha in shas:
            trash = self.trash_path(sha)
            live = self.store.path(sha)
            entry = self.ledger.entry(sha)
            if trash.is_file():
                data = trash.read_bytes()
                if hashlib.sha256(data).hexdigest() != sha:
                    raise CorruptionError('对象摘要损坏，阻断恢复', sha, 'trash')
                if live.exists():
                    if live.read_bytes() != data:
                        raise ReclaimConflict('同一地址已存在不同内容，拒绝覆盖：' + sha)
                    trash.unlink()
                else:
                    live.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(trash, live)
                self.hooks.fire('restore.after_move', sha=sha)
                if entry is not None and entry['state'] == 'trashed':
                    self.ledger.set_entry_state(sha, 'restored', '于逻辑时钟%d恢复' % now)
                self.hooks.fire('restore.after_entry', sha=sha)
                restored.append(sha)
            elif live.is_file():
                if entry is not None and entry['state'] == 'trashed':
                    self.ledger.set_entry_state(sha, 'restored', '中断续接补账')
                restored.append(sha)
            else:
                raise CorruptionError('对象缺失，无法恢复', sha, 'live/trash')
        return restored

    # ---- 清除 ----

    def purge(self):
        """宽限期后清除：重新遍历当前有效根（含trash），重新被引用的对象恢复而非删除。"""
        now = self.clock()
        roots, default_grace = valid_roots(self.registry.document(), now)
        self._verify_space('live')
        trash_sizes = self._verify_space('trash')
        reachable = self._traverse([root['object'] for root in roots], spaces=('live', 'trash'))
        restored, purged, kept = [], [], []
        for sha in sorted(trash_sizes):
            if sha in reachable:
                self.restore([sha])
                self.hooks.fire('purge.after_restore', sha=sha)
                restored.append(sha)
                continue
            entry = self.ledger.entry(sha)
            trashed_at = entry['trashed_at'] if entry is not None else now
            grace = entry['grace'] if entry is not None else default_grace
            if now < trashed_at + grace:
                kept.append(sha)
                continue
            self.trash_path(sha).unlink()
            self.hooks.fire('purge.after_delete', sha=sha)
            if entry is not None:
                self.ledger.set_entry_state(sha, 'purged', '于逻辑时钟%d清除' % now)
            self.hooks.fire('purge.after_entry', sha=sha)
            purged.append(sha)
        reconciled = []
        for entry in self.ledger.entries(state='trashed'):
            sha = entry['sha']
            if not self.trash_path(sha).is_file() and not self.store.path(sha).is_file():
                self.ledger.set_entry_state(sha, 'purged', '文件已不存在，续接补账')
                reconciled.append(sha)
        return {'restored': restored, 'purged': purged, 'kept': kept, 'reconciled': reconciled,
                'purged_bytes': sum(trash_sizes[sha] for sha in purged)}

    # ---- 统计 ----

    def stats(self):
        """可观察字节统计：live/trash实际占用与账本各状态，主键去重不重复计量。"""
        live = self._verify_space('live')
        trash = self._verify_space('trash')
        states = {}
        for entry in self.ledger.entries():
            bucket = states.setdefault(entry['state'], {'objects': 0, 'bytes': 0})
            bucket['objects'] += 1
            bucket['bytes'] += entry['size']
        return {
            'live': {'objects': len(live), 'bytes': sum(live.values())},
            'trash': {'objects': len(trash), 'bytes': sum(trash.values())},
            'ledger': states,
            'reclaimed_bytes': states.get('purged', {}).get('bytes', 0),
        }


def load_sample_roots(samples, seed):
    """把样本根记录中的逻辑名称映射为真实内容地址，生成根集合文档。"""
    samples = Path(samples)
    roots = read_json(samples / 'roots.json')
    pins = read_json(samples / 'pins.json')
    retention = read_json(samples / 'retention.json')

    def address(name):
        if name not in seed:
            raise ValueError('样本引用了未知对象名：' + str(name))
        return seed[name]

    document = {
        'snapshots': [{'id': row['id'], 'object': address(row['object_name']),
                       'expires': int(row['expires']), 'hold': bool(row['hold'])} for row in roots],
        'pins': [{'id': row['id'], 'object': address(row['object_name']),
                  'expires': int(row['expires'])} for row in pins],
        'retention': {'trash_grace': int(retention[0]['trash_grace'])},
    }
    _validate_roots(document)
    return document

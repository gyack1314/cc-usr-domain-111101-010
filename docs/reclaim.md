# 可恢复对象回收

datapack/reclaim.py 在内容对象库上提供可恢复回收：先证明对象不被任何有效根引用，
再移入隔离区并写持久回收账，过了保留期并重新确认无引用才真正清除。全程只移动
对象位置，不修改任何已发布清单或数据内容；不连接真实云存储。

## 根集合与逻辑时钟

根集合登记处 RootRegistry 用 RevisionStore 单流保存 `{snapshots, pins, retention}`，
每次写入推进单调版本号，作为计划围栏。有效根按调用方注入的逻辑时钟判定：

- 未过期快照：`expires > now`；`expires == now` 即已过期。
- 人工保留项：快照记录 `hold` 为真，不看 `expires`。
- 读者pin：`expires > now` 才有效，过期读者不算根。

`retention.trash_grace` 是隔离宽限期秒数，隔离时冻结进回收账。

## 计划 plan()

全量核验 live 空间每个对象的摘要与清单结构，再从有效根迭代遍历（显式栈，深层
清单不溢出；重复与菱形引用只展开一次、只计量一次）。计划记录根集合版本、根集合
摘要、有效根、可回收对象、字节数及理由，文件内容寻址存于 `gc/plans/`。

缺对象、摘要不符或清单解析失败会抛 CorruptionError 并指出损坏位置（对象地址与
所在空间），本轮阻断；失败的遍历不会被当成无引用。

## 隔离 isolate(plan_id)

先核对计划围栏：计划之后有快照新增、保留变更或读者续租时，根集合版本已推进，
抛 StalePlan，旧计划必须重建，不允许先删完再发现根变了。核对通过后把可回收对象
逐个移入 `trash/` 并写回收账（SQLite，对象地址为主键，不重复计量）。同一地址已
存在不同内容时抛 ReclaimConflict，拒绝覆盖。

## 恢复 restore(shas)

保留期内把隔离对象恢复到原内容地址，恢复前核验摘要；已在原位的重复调用安全。

## 清除 purge()

宽限期（`trashed_at + grace`）到期后才处理，且重新遍历当前有效根（含 trash 空间）：
重新被引用的对象恢复而非清除；仍无引用的才删除并销账。purge 不依赖旧计划，围栏
由这次重新遍历承担。

## 中断续接

目录移动与账本写入之间、删除与销账之间中断后，重复调用 isolate/restore/purge 会
按文件实际位置识别现场：已移动未记账的补账，已删除未销账的销账，不重复移动、
不重复计量。测试与演示用 BreakpointHooks 注入中断点：

`isolate.after_move`、`isolate.after_entry`、`restore.after_move`、`restore.after_entry`、
`purge.after_restore`、`purge.after_delete`、`purge.after_entry`。

## 字节统计 stats()

返回 live 与 trash 的对象数、字节数（每次实际核验磁盘），以及回收账各状态
（trashed/restored/purged）的对象数与字节数和 reclaimed_bytes。

## 演示与接口

```bash
python examples/demo_reclaim.py        # 共享对象保留、新pin使旧计划失败、隔离恢复、中断续接
python -m datapack.cli gc-plan 目录 --clock 120
python -m datapack.cli gc-isolate 目录 计划ID --clock 120
python -m datapack.cli gc-restore 目录 对象地址... --clock 125
python -m datapack.cli gc-purge 目录 --clock 200
python -m datapack.cli gc-stats 目录
```

未登记根集合前所有对象都视为无根保护，请勿直接 isolate。样本根记录
（samples/roots.json、pins.json、retention.json）可用 load_sample_roots 映射为根集合文档。

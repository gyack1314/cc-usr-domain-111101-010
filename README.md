# 共享数据快照的可恢复对象回收数据包

内容寻址资料库，已有不可变对象读写与清单编码；根目录记录由业务端单独维护。 数据文件位于samples，字段和边界见docs/domain.md，持久目录接口见docs/storage.md。

## 本地运行

Python 3.11以上，仅依赖标准库，无需真实账号或外部数据服务。

```bash
python -m datapack.cli inspect samples
python -m unittest discover -s tests -v
python -m compileall -q datapack tests
```

第一条命令检查已有资料，第二条运行原有读取、内容存储和领域原语检查，第三条构建Python字节码。

## 容器

```bash
docker build -t local-datapack .
docker run --rm --network none local-datapack
docker run --rm --network none local-datapack python -m unittest discover -s tests -v
```

应用没有必须常驻的服务，入口和库可在同一容器离线运行。state和output是本地工作目录，已排除出Git。样本文件只读，修订目录使用独立SQLite文件，旧版本可继续查询。

## 目录

datapack/storage.py提供摘要、原子文件与修订目录；datapack/records.py提供文件读取及重复主键分类；datapack/domain.py提供当前领域原语；tests验证这些已有行为。数据加工结果不由资料检查命令自动生成。

`python -m datapack.seed state/store` 建立一个有共享内容和孤立对象的本地资料库，seed.json把样本根名称映射到真实内容地址。重复初始化拒绝覆盖，不执行任何回收。

## 可恢复对象回收

datapack/reclaim.py提供计划、隔离、恢复与清除接口：先证明对象不被任何有效根引用，再移入trash并写持久回收账，过了保留期并重新确认无引用才清除；接口约定见docs/reclaim.md。

```bash
python examples/demo_reclaim.py
python -m datapack.cli gc-plan state/store --clock 120
python -m datapack.cli gc-stats state/store
```

演示覆盖共享对象保留、新pin使旧计划失败、隔离恢复和隔离中断后接续，全部使用合成对象离线运行。

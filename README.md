# caskpatch —— 隔离档案柜磁盘索引修复复核工具

离线、纯 Python 3.12 标准库(Windows 普通 CPU 可运行),UTF-8 JSON CLI。
输入是两个合成清单(各描述一棵分块树)及其操作追踪,用于复核磁盘索引修复
过程在任意断电点下的安全性,并为修复动作排出可恢复的执行计划。

## 模型约定

- **清单(manifest)**:描述一棵分块树。槽位(slot)以稳定 ID 区分;`index`
  槽位有 `children`,`block` 槽位带 `content`/`digest`/`partition`/`size`。
- **去重**:块按 `(内容摘要, 保护标签)` 去重;同一(更强)分区可并存不同原材料的
  同摘要块,不同保护标签互不洗掉。槽位各自稳定,可多个槽位指向同一实例。
- **保护传播**:有效保护等级 = max(自身, 所有引用链祖先的有效等级)。
  低保护分区不能暂存从高保护域传来的块;分区还有写权限与固定容量。
- **原子替换**:引用同一条索引的档案版本必须对该索引整体原子替换
  (`commit_root` 按索引切换可见版本)。
- **断电语义**:过程分为 写候选块 → 更新根引用 → 释放旧块 三个阶段,
  任一阶段断电后只能看见完整旧版或完整新版;除非原日志显式记录旧版损坏
  (`old_corrupted: true`),此时旧版残缺被容忍并仅作记录。

## 输入格式(案例 JSON)

```jsonc
{
  "case_id": "隔离档案柜-ok-01",
  "old_corrupted": false,                 // 可选:原日志已显式记录旧版损坏
  "partitions": [
    {"id": "P0", "protection": 0, "writable": true, "capacity": 10}
  ],
  "manifests": {
    "old": {"roots": {"main": "r"}, "slots": {
      "r": {"kind": "index", "children": ["a"], "protection": 0},
      "a": {"kind": "block", "content": "alpha",
            "digest": "sha256:…", "partition": "P0", "protection": 0, "size": 1}}},
    "new": { "…": "同结构;可另有 fix 等更多版本" }
  },
  "retained_snapshots": [{"version": "old", "index": "main"}],  // 可选,钉住旧块
  "trace": [                            // check/verify 复核的操作日志
    {"id": "t1", "op": "stage", "slot": "b2", "partition": "P1",
     "version": "new", "digest": "可选:实际写入的摘要(缺省=声明值)"},
    {"id": "t2", "op": "commit_root", "index": "main", "to": "new"},
    {"id": "t3", "op": "release", "slot": "b"}
  ],
  "switches": [                         // arrange 的输入,最多 16 个分块切换
    {"action_id": "sw_a", "index": "i1", "slot": "n1",
     "old_slot": "o1", "home": "PA"}
  ]
}
```

## 命令

```console
python caskpatch.py check CASE [TRACE]   # 复核追踪;TRACE 可覆盖案例内 trace
python caskpatch.py arrange CASE         # 选择切换顺序与暂存目标
python caskpatch.py verify CASE          # 复算清单并生成两类反例
python caskpatch.py db-create DB CASE    # 从案例创建本地数据库
python caskpatch.py commit DB PLAN [--pause]  # 三阶段提交(可断电)
python caskpatch.py inspect DB           # 重开数据库并恢复,输出当前状态
```

退出码:`0` 正常;`check` 发现不安全断点返回 `3`;`arrange` 不可行返回 `4`;
输入错误返回 `2`(错误以 JSON 打印)。

### check 输出

逐步重算:`visible_roots`、`stored_instances`、`unique_blocks`、
`duplicate_refs`(唯一块重复计数)、`space_by_partition`;并给出
`first_unsafe`(最早不安全断点:步骤、原因、**引用链**)与 `space_peak`。
不安全判定包括:提交前释放仍被可见根/保留快照引用的块、提交时新树缺块、
摘要不符、越权/低保护暂存、容量超限。

### arrange 输出与优化次序

同一索引的切换必须落在同一次原子提交(索引原子性)。计划按
**① 附加空间峰值最小 → ② 跨域操作最少 → ③ 动作 ID 字典序** 生成:
逐组提交、提交后立即释放已死旧块使峰值最小化;整组能放回家分区的组优先
(0 跨域);平局按 `action_id`。输出含 `metrics`(附加空间峰值、跨域操作数、
切换顺序)与 `verification`(用 check 引擎复算该计划,确认每个断点可恢复、
所有保留快照满足)。不可行时返回 `infeasible` 与原因。

### verify 输出

- `manifest_digest_audit`:重算每个块的内容摘要并与声明值比对;
- `trace_audit`:对案例内追踪做完整 check;
- `counterexamples`:两类确定性反例——
  1. `midway-digest`:终态摘要全部一致、看似合理,但中途可见根曾引用
     错误摘要的块(只看提交后状态会漏判);
  2. `shared-old-early-release`:两次原子提交共享旧块 X,第一次提交后
     提前释放 X,第二次提交前断电导致回滚失败(含 `crash_simulation`)。

## 本地数据库与断电恢复

`db-create` 在目录内写 `db.json`;`commit` 用写前日志 `journal.json`
记录三阶段进度,所有落盘均为同目录临时文件 + 原子替换:

1. `staged`:写日志 → 候选块落库;
2. `root`:推进日志 → 原子更新根引用、候选块转正;
3. `released`:推进日志 → 释放旧块 → 删除日志。

`inspect`(以及任何打开)先执行恢复:日志停在 `staged` 则回滚(只见完整
旧版);停在 `root`/`released` 则幂等前滚(只见完整新版)。
`commit --pause` 在每阶段打印 `PHASE <name>` 并等待一行输入,测试据此在
三个阶段分别 `kill` 子进程,再重开数据库验证一致性。

## 目录结构

```
caskpatch.py          CLI 入口
caskpatch_core.py     清单模型 / check / arrange / verify
caskpatch_db.py       本地数据库与三阶段提交恢复
fixtures/             合成夹具(tools/gen_fixtures.py 生成,摘要真实可复算)
tests/test_caskpatch.py
```

## 测试

```console
python -m unittest discover -s tests -v
```

覆盖:安全/不安全追踪、保护与容量违规、arrange 三项优化次序与保留快照、
verify 两类反例、以及在 staged/root/released 三个阶段杀掉子进程后重开
数据库的恢复一致性。

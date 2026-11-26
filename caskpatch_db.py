"""caskpatch 本地数据库:单目录 JSON 存储 + 写前日志,三阶段断电可恢复。

提交分为三个阶段,任一阶段断电后重开数据库只能看到完整旧版或完整新版:
  1. staged   —— 写候选块(先写日志,再落库);
  2. root     —— 原子更新根引用(先推进日志,再落库);
  3. released —— 释放旧块,最后删除日志。
恢复规则:日志停在 staged 则回滚(丢弃候选块);停在 root/released 则前滚
(补全根引用与释放,幂等)。若日志显式记录旧版损坏(old_corrupted),
恢复时仅作记录,不视为不一致。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PHASES = ("staged", "root", "released")


class LocalDB:
    def __init__(self, path):
        self.dir = Path(path)
        self.db_file = self.dir / "db.json"
        self.journal_file = self.dir / "journal.json"

    # ---- 基本读写 ----

    @staticmethod
    def _write_json(path, obj):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)  # 同目录原子替换

    def _read(self):
        return json.loads(self.db_file.read_text(encoding="utf-8"))

    @classmethod
    def create(cls, path, case):
        self = cls(path)
        self.dir.mkdir(parents=True, exist_ok=True)
        old = case["manifests"]["old"]
        blocks = {}
        for sid, s in old.get("slots", {}).items():
            if s.get("kind") == "block":
                blocks[sid] = {"digest": s.get("digest"), "content": s.get("content"),
                               "partition": s.get("partition"),
                               "protection": s.get("protection", 0),
                               "size": s.get("size", 1)}
        data = {"format": 1,
                "roots": {i: "old" for i in old.get("roots", {})},
                "blocks": blocks,
                "candidates": {},
                "manifests": case["manifests"]}
        self._write_json(self.db_file, data)
        return self

    # ---- 崩溃恢复 ----

    def recover(self):
        """重开数据库时调用;返回恢复信息,无日志返回 None。"""
        if not self.journal_file.exists():
            return None
        j = json.loads(self.journal_file.read_text(encoding="utf-8"))
        data = self._read()
        phase = j.get("phase")
        if phase == "staged":
            # 根引用尚未更新:回滚,丢弃候选块,只见完整旧版
            for sid in j.get("added", []):
                data["candidates"].pop(sid, None)
            self._write_json(self.db_file, data)
            self.journal_file.unlink()
            return {"recovery": "rollback", "phase": phase,
                    "tolerated_old_corruption": bool(j.get("old_corrupted"))}
        if phase in ("root", "released"):
            # 根引用已更新:前滚,补全候选块转正与旧块释放(幂等),只见完整新版
            data["roots"] = dict(j["roots_after"])
            for sid, rec in j.get("promote", {}).items():
                data["blocks"][sid] = rec
            data["candidates"] = {}
            for sid in j.get("releases", []):
                data["blocks"].pop(sid, None)
            self._write_json(self.db_file, data)
            self.journal_file.unlink()
            return {"recovery": "roll-forward", "phase": phase,
                    "tolerated_old_corruption": bool(j.get("old_corrupted"))}
        raise ValueError(f"未知日志阶段 {phase!r}")

    # ---- 三阶段提交 ----

    def commit(self, plan, pause=False, out=None, inp=None):
        """执行计划。pause=True 时每阶段打印 'PHASE <name>' 并等待一行输入,
        供测试在阶段边界杀掉子进程。"""
        out = out if out is not None else sys.stdout
        inp = inp if inp is not None else sys.stdin
        self.recover()  # 先清理上次残骸
        data = self._read()
        mans = data["manifests"]
        actions = plan.get("actions") or plan.get("plan", {}).get("actions")
        if not actions:
            raise ValueError("计划中没有动作")

        roots_after = dict(data["roots"])
        promote = {}
        releases = []
        for a in actions:
            op = a.get("op")
            if op == "stage":
                ver = a.get("version", "new")
                sid = a["slot"]
                s = mans[ver]["slots"][sid]
                promote[sid] = {"digest": a.get("digest", s.get("digest")),
                                "content": s.get("content"),
                                "partition": a.get("partition", s.get("partition")),
                                "protection": s.get("protection", 0),
                                "size": s.get("size", 1)}
            elif op == "commit_root":
                roots_after[a["index"]] = a["to"]
            elif op == "release":
                releases.append(a["slot"])

        journal = {"phase": "staged", "added": list(promote), "promote": promote,
                   "releases": releases, "roots_after": roots_after,
                   "old_corrupted": bool(plan.get("old_corrupted"))}

        def mark(name):
            print(f"PHASE {name}", file=out, flush=True)
            if pause:
                inp.readline()

        # 阶段 1:写候选块(日志先行)
        self._write_json(self.journal_file, journal)
        data["candidates"].update(promote)
        self._write_json(self.db_file, data)
        mark("staged")

        # 阶段 2:原子更新根引用,候选块转正
        journal["phase"] = "root"
        self._write_json(self.journal_file, journal)
        data = self._read()
        data["roots"] = roots_after
        data["blocks"].update(promote)
        data["candidates"] = {}
        self._write_json(self.db_file, data)
        mark("root")

        # 阶段 3:释放旧块,删除日志
        journal["phase"] = "released"
        self._write_json(self.journal_file, journal)
        data = self._read()
        for sid in releases:
            data["blocks"].pop(sid, None)
        self._write_json(self.db_file, data)
        mark("released")

        self.journal_file.unlink()
        print("DONE", file=out, flush=True)
        return {"status": "committed", "roots": roots_after}

    # ---- 查看 ----

    def inspect(self):
        rec = self.recover()
        data = self._read()
        return {"roots": data["roots"],
                "blocks": sorted(data["blocks"]),
                "candidates": sorted(data["candidates"]),
                "recovered": rec}

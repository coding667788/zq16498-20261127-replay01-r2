"""caskpatch 核心:分块树清单模型、追踪复核(check)、排程(arrange)、复算(verify)。

术语与规则:
- 清单(manifest)描述一棵分块树;槽位(slot)以稳定 ID 区分,块槽位带内容摘要。
- 块按 (内容摘要, 保护标签) 去重;同一分区可并存同摘要不同标签的块,互不洗掉。
- 保护等级沿引用链向下传播:子节点有效等级 = max(自身, 所有父节点有效等级)。
- 分区有写权限与固定容量;低保护分区不能暂存高保护域传来的块。
- 断电安全:写候选块 -> 更新根引用 -> 释放旧块,任一阶段断电后只能看到
  完整旧版或完整新版,除非原日志显式记录旧版损坏(old_corrupted)。
"""
from __future__ import annotations

import hashlib

OLD = "old"


class CaseError(Exception):
    """输入清单或追踪不符合约定格式。"""


def digest_of(content):
    """内容摘要:sha256,带算法前缀。"""
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _slots(manifest):
    return manifest.get("slots", {})


def _children(slot):
    return slot.get("children", []) or []


def block_slots(manifest):
    return {sid: s for sid, s in _slots(manifest).items() if s.get("kind") == "block"}


def effective_protection(manifest):
    """保护等级沿引用链向下传播(不动点迭代,分块树为 DAG)。"""
    slots = _slots(manifest)
    eff = {sid: int(s.get("protection", 0)) for sid, s in slots.items()}
    for _ in range(len(slots) + 1):
        changed = False
        for sid, s in slots.items():
            for ch in _children(s):
                if ch in eff and eff[ch] < eff[sid]:
                    eff[ch] = eff[sid]
                    changed = True
        if not changed:
            break
    return eff


def reachable(manifest, root_sid):
    slots = _slots(manifest)
    seen, stack = set(), [root_sid]
    while stack:
        sid = stack.pop()
        if sid in seen or sid not in slots:
            continue
        seen.add(sid)
        stack.extend(_children(slots[sid]))
    return seen


def reference_chain(manifest, target_sid, index=None):
    """从根到 target 的一条引用链(BFS 最短)。找不到返回 None。"""
    slots = _slots(manifest)
    roots = manifest.get("roots", {})
    if index and index in roots:
        starts = [roots[index]]
    else:
        starts = list(roots.values())
    queue = [(r, [r]) for r in starts]
    seen = set(starts)
    while queue:
        sid, path = queue.pop(0)
        if sid == target_sid:
            return path
        for ch in _children(slots.get(sid, {})):
            if ch in slots and ch not in seen:
                seen.add(ch)
                queue.append((ch, path + [ch]))
    return None


class Engine:
    """逐步重放追踪,维护可见根、存储实例与空间用量。

    存储实例按 (分区, 摘要, 保护标签) 去重;槽位各自稳定,多个槽位可指向同一实例。
    """

    def __init__(self, case):
        self.case = case
        self.partitions = {p["id"]: dict(p) for p in case.get("partitions", [])}
        self.manifests = case.get("manifests", {})
        if OLD not in self.manifests:
            raise CaseError("case.manifests 必须包含 'old' 版本")
        self.eff = {v: effective_protection(m) for v, m in self.manifests.items()}
        self.visible = {idx: OLD for idx in self.manifests[OLD].get("roots", {})}
        self.stored = {}     # slot -> {partition,digest,protection,size,version}
        self.instances = {}  # (partition,digest,protection) -> {size, slots:set}
        self.baseline_violations = []
        for ver, idx in self._initial_versions():
            man = self.manifests.get(ver)
            if man is None:
                raise CaseError(f"保留快照引用了未知版本 {ver!r}")
            root = man.get("roots", {}).get(idx)
            if root is None:
                raise CaseError(f"保留快照引用了未知索引 {idx!r} (版本 {ver!r})")
            for sid in reachable(man, root):
                s = _slots(man).get(sid, {})
                if s.get("kind") == "block":
                    self._store(sid, s.get("partition"), s.get("digest"),
                                self.eff[ver][sid], int(s.get("size", 1)), ver)
        self._check_capacity(None, self.baseline_violations)

    def _initial_versions(self):
        pairs = [(OLD, idx) for idx in self.visible]
        for snap in self.case.get("retained_snapshots", []):
            pairs.append((snap["version"], snap["index"]))
        seen, out = set(), []
        for p in pairs:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    # ---- 存储原语 ----

    def _store(self, slot, partition, digest, protection, size, version):
        if slot in self.stored:
            return
        key = (partition, digest, protection)
        self.stored[slot] = {"partition": partition, "digest": digest,
                             "protection": protection, "size": size, "version": version}
        inst = self.instances.setdefault(key, {"size": size, "slots": set()})
        inst["slots"].add(slot)

    def _drop(self, slot):
        rec = self.stored.pop(slot)
        key = (rec["partition"], rec["digest"], rec["protection"])
        inst = self.instances[key]
        inst["slots"].discard(slot)
        if not inst["slots"]:
            del self.instances[key]

    def usage(self):
        u = {pid: 0 for pid in self.partitions}
        for (pid, _d, _l), inst in self.instances.items():
            u[pid] = u.get(pid, 0) + inst["size"]
        return u

    def live_refs(self):
        """当前可见根与所有保留快照的可达槽位集合。"""
        live = set()
        for idx, ver in self.visible.items():
            man = self.manifests[ver]
            root = man.get("roots", {}).get(idx)
            if root:
                live |= reachable(man, root)
        for snap in self.case.get("retained_snapshots", []):
            man = self.manifests[snap["version"]]
            root = man.get("roots", {}).get(snap["index"])
            if root:
                live |= reachable(man, root)
        return live

    # ---- 违规记录 ----

    def _v(self, reason, action, slot, version, **kw):
        d = {"reason": reason, "action_id": action.get("id") if action else None,
             "slot": slot, "version": version,
             "reference_chain": reference_chain(self.manifests[version], slot)
             if version in self.manifests else None}
        d.update(kw)
        return d

    def _check_capacity(self, action, out, only=None):
        for pid, used in self.usage().items():
            p = self.partitions.get(pid)
            if p is None:
                continue
            cap = int(p.get("capacity", 0))
            if used > cap and (only is None or pid == only):
                out.append({"reason": "capacity-exceeded",
                            "action_id": action.get("id") if action else None,
                            "slot": None, "version": None, "reference_chain": None,
                            "partition": pid, "usage": used, "capacity": cap})

    # ---- 动作 ----

    def apply(self, action):
        """应用一个动作,返回 (violations, notes)。"""
        op = action.get("op")
        if op == "stage":
            return self._stage(action)
        if op == "commit_root":
            return self._commit(action)
        if op == "release":
            return self._release(action)
        raise CaseError(f"未知动作: {op!r}")

    def _stage(self, a):
        v, notes = [], []
        ver = a.get("version", "new")
        man = self.manifests.get(ver)
        sid = a.get("slot")
        if man is None or sid not in _slots(man):
            raise CaseError(f"stage 引用了未知槽位 {sid!r} (版本 {ver!r})")
        slot = _slots(man)[sid]
        if slot.get("kind") != "block":
            raise CaseError(f"stage 目标 {sid!r} 不是块槽位")
        declared = slot.get("digest")
        written = a.get("digest", declared)
        content = slot.get("content")
        if content is not None and declared != digest_of(content):
            v.append(self._v("content-digest-mismatch", a, sid, ver,
                             declared=declared, recomputed=digest_of(content)))
        if written != declared:
            v.append(self._v("digest-mismatch", a, sid, ver,
                             declared=declared, written=written))
        req = self.eff[ver][sid]
        pid = a.get("partition")
        part = self.partitions.get(pid)
        if part is None:
            v.append(self._v("unknown-partition", a, sid, ver, partition=pid))
        else:
            if not part.get("writable", False):
                v.append(self._v("partition-not-writable", a, sid, ver, partition=pid))
            if int(part.get("protection", 0)) < req:
                # 低保护域不能暂存从高保护域传来的块
                v.append(self._v("protection-violation", a, sid, ver, partition=pid,
                                 required=req, actual=int(part.get("protection", 0))))
        size = int(slot.get("size", 1))
        # 按日志实际写入继续模拟,以暴露后续错误
        self._store(sid, pid, written, req, size, ver)
        self._check_capacity(a, v, only=pid)
        return v, notes

    def _commit(self, a):
        v, notes = [], []
        idx = a.get("index")
        to = a.get("to")
        man = self.manifests.get(to)
        if man is None or idx not in man.get("roots", {}):
            raise CaseError(f"commit_root 引用了未知索引 {idx!r} (版本 {to!r})")
        if idx not in self.visible:
            raise CaseError(f"commit_root 引用了未登记的索引 {idx!r}")
        root = man["roots"][idx]
        for sid in sorted(reachable(man, root)):
            s = _slots(man).get(sid, {})
            if s.get("kind") != "block":
                continue
            rec = self.stored.get(sid)
            if rec is None:
                v.append(self._v("commit-missing-block", a, sid, to, index=idx))
            elif rec["digest"] != s.get("digest"):
                v.append(self._v("committed-digest-mismatch", a, sid, to, index=idx,
                                 declared=s.get("digest"), stored=rec["digest"]))
        # 原子替换:同一索引的档案版本整体切换
        self.visible[idx] = to
        return v, notes

    def _release(self, a):
        v, notes = [], []
        sid = a.get("slot")
        if sid not in self.stored:
            v.append({"reason": "release-unknown-slot", "action_id": a.get("id"),
                      "slot": sid, "version": None, "reference_chain": None})
            return v, notes
        if sid in self.live_refs():
            chain, where = None, None
            for idx, ver in self.visible.items():
                man = self.manifests[ver]
                root = man.get("roots", {}).get(idx)
                if root and sid in reachable(man, root):
                    chain = reference_chain(man, sid, idx)
                    where = f"{ver}:{idx}"
                    break
            if chain is None:
                for snap in self.case.get("retained_snapshots", []):
                    man = self.manifests[snap["version"]]
                    root = man.get("roots", {}).get(snap["index"])
                    if root and sid in reachable(man, root):
                        chain = reference_chain(man, sid, snap["index"])
                        where = f"snapshot:{snap['version']}:{snap['index']}"
                        break
            rec = {"reason": "premature-release", "action_id": a.get("id"),
                   "slot": sid, "version": None, "referenced_by": where,
                   "reference_chain": chain}
            if self.case.get("old_corrupted") or a.get("tolerate_corrupted_old"):
                notes.append({**rec, "severity": "tolerated",
                              "note": "原日志已显式记录旧版损坏,容忍旧版残缺"})
            else:
                v.append(rec)
        self._drop(sid)
        return v, notes

    # ---- 指标 ----

    def metrics(self):
        u = self.usage()
        unique = {(d, l) for (_p, d, l) in self.instances}
        refs = sum(len(i["slots"]) for i in self.instances.values())
        return {"visible_roots": dict(self.visible),
                "stored_instances": len(self.instances),
                "unique_blocks": len(unique),
                "duplicate_refs": refs - len(unique),
                "space_by_partition": u,
                "space_total": sum(u.values())}


def run_check(case, trace=None):
    """check CASE TRACE:逐步重算可见根与唯一块重复计数,定位最早不安全断点。"""
    eng = Engine(case)
    actions = case.get("trace", []) if trace is None else trace
    steps = []
    first = None
    baseline = eng.metrics()
    peak, peak_step = baseline["space_total"], -1
    if eng.baseline_violations:
        w = eng.baseline_violations[0]
        first = {"step": None, "action_id": None, "reason": w["reason"],
                 "slot": None, "reference_chain": None, "detail": w}
    for i, a in enumerate(actions):
        viols, notes = eng.apply(a)
        m = eng.metrics()
        steps.append({"step": i, "action_id": a.get("id"), "op": a.get("op"),
                      "violations": viols, "notes": notes, **m})
        if m["space_total"] > peak:
            peak, peak_step = m["space_total"], i
        if first is None and viols:
            w = viols[0]
            first = {"step": i, "action_id": a.get("id"), "reason": w["reason"],
                     "slot": w.get("slot"), "reference_chain": w.get("reference_chain"),
                     "detail": w}
    status = "unsafe" if first else "ok"
    return {"case_id": case.get("case_id"),
            "status": status,
            "baseline": baseline,
            "baseline_violations": eng.baseline_violations,
            "steps": steps,
            "first_unsafe": first,
            "space_peak": {"value": peak, "step": peak_step},
            "final": eng.metrics()}


def run_arrange(case):
    """arrange CASE:为不超过 16 个分块切换选择顺序与暂存目标。

    约束:同一索引的切换必须落在同一次原子提交;每个断点均可恢复;
    所有保留快照引用的块不得释放。
    优化次序:1) 附加空间峰值最小;2) 跨域操作最少;3) 动作 ID 字典序。
    """
    switches = list(case.get("switches", []))
    if not switches:
        raise CaseError("arrange 需要至少一个分块切换 (switches)")
    if len(switches) > 16:
        raise CaseError("分块切换最多 16 个")
    manifests = case.get("manifests", {})
    if "new" not in manifests:
        raise CaseError("arrange 需要 manifests.new")
    new_man = manifests["new"]
    eng = Engine(case)
    eff_new = eng.eff["new"]
    parts = eng.partitions

    groups = {}
    for sw in switches:
        slot = new_man.get("slots", {}).get(sw.get("slot"))
        if not slot or slot.get("kind") != "block":
            raise CaseError(f"切换 {sw.get('action_id')!r} 引用了未知块槽位 {sw.get('slot')!r}")
        groups.setdefault(sw["index"], []).append(sw)
    for members in groups.values():
        members.sort(key=lambda s: s["action_id"])
    remaining = sorted(groups.values(), key=lambda m: min(s["action_id"] for s in m))

    def free_now():
        u = eng.usage()
        return {pid: int(p.get("capacity", 0)) - u.get(pid, 0) for pid, p in parts.items()}

    def place_group(members, home_only):
        """为整组选择暂存目标;成功返回 [(sw, pid)],失败返回 None。"""
        free = free_now()
        chosen = []
        for sw in members:
            sid = sw["slot"]
            slot = new_man["slots"][sid]
            req = eff_new[sid]
            size = int(slot.get("size", 1))
            home = sw.get("home") or slot.get("partition")
            order = [home] if home_only else [home] + sorted(
                (p for p in parts if p != home),
                key=lambda p: (int(parts[p].get("protection", 0)), p))
            picked = None
            for pid in order:
                p = parts.get(pid)
                if not p or not p.get("writable") or int(p.get("protection", 0)) < req:
                    continue
                need = 0 if (pid, slot.get("digest"), req) in eng.instances else size
                if free.get(pid, 0) >= need:
                    picked = pid
                    free[pid] = free.get(pid, 0) - need
                    break
            if picked is None:
                return None
            chosen.append((sw, picked))
        return chosen

    actions = []
    cross = 0
    switch_order = []
    while remaining:
        plan = chosen_members = None
        for members in remaining:  # 优先整组都能放回家分区的组(0 跨域)
            plan = place_group(members, home_only=True)
            if plan:
                chosen_members = members
                break
        if plan is None:
            for members in remaining:  # 退而求其次:任意合法暂存目标
                plan = place_group(members, home_only=False)
                if plan:
                    chosen_members = members
                    break
        if plan is None:
            if case.get("old_corrupted"):
                # 旧版已显式记录损坏:允许先释放该组旧块再暂存
                for sw in remaining[0]:
                    old_slot = sw.get("old_slot")
                    if old_slot and old_slot in eng.stored:
                        act = {"id": f'{sw["action_id"]}-prerelease', "op": "release",
                               "slot": old_slot, "tolerate_corrupted_old": True}
                        actions.append(act)
                        eng.apply(act)
                continue
            return {"case_id": case.get("case_id"), "status": "infeasible",
                    "reason": "容量不足:没有任何一组切换可以找到合法暂存目标",
                    "remaining_groups": [[s["action_id"] for s in m] for m in remaining],
                    "partial_actions": actions}
        idx = None
        for sw, pid in plan:
            idx = sw["index"]
            act = {"id": f'{sw["action_id"]}-stage', "op": "stage",
                   "slot": sw["slot"], "partition": pid, "version": "new"}
            actions.append(act)
            eng.apply(act)
            home = sw.get("home") or new_man["slots"][sw["slot"]].get("partition")
            if pid != home:
                cross += 1
            switch_order.append(sw["action_id"])
        commit_id = min(s["action_id"] for s, _ in plan)
        act = {"id": f"{commit_id}-commit", "op": "commit_root", "index": idx, "to": "new"}
        actions.append(act)
        eng.apply(act)
        live = eng.live_refs()
        for sid in sorted(s for s, rec in eng.stored.items()
                          if rec["version"] == OLD and s not in live):
            act = {"id": f"release-{sid}", "op": "release", "slot": sid}
            actions.append(act)
            eng.apply(act)
        remaining = [m for m in remaining if m is not chosen_members]

    report = run_check(case, trace=actions)
    ok = report["status"] == "ok"
    return {
        "case_id": case.get("case_id"),
        "status": "ok" if ok else "unsafe",
        "plan": {"actions": actions},
        "metrics": {
            "space_peak_additional":
                report["space_peak"]["value"] - report["baseline"]["space_total"],
            "cross_domain_ops": cross,
            "switch_order": switch_order,
            "action_order": [a["id"] for a in actions],
        },
        "verification": {
            "status": report["status"],
            "first_unsafe": report["first_unsafe"],
            "snapshots_satisfied": not any(
                v["reason"] == "premature-release"
                for s in report["steps"] for v in s["violations"]),
            "all_breakpoints_safe": ok,
        },
    }


def _replay_prefix(case, trace, upto):
    eng = Engine(case)
    for a in trace[: upto + 1]:
        eng.apply(a)
    return eng


def _kind_of(case, sid):
    for man in case["manifests"].values():
        s = man.get("slots", {}).get(sid)
        if s:
            return s.get("kind")
    return None


def _final_state_digest_ok(case):
    """只看终态:所有库存块的摘要与清单声明及内容一致。"""
    eng = Engine(case)
    for a in case.get("trace", []):
        eng.apply(a)
    for sid, rec in eng.stored.items():
        slot = case["manifests"][rec["version"]].get("slots", {}).get(sid, {})
        if slot.get("digest") != rec["digest"]:
            return False
        content = slot.get("content")
        if content is not None and digest_of(content) != rec["digest"]:
            return False
    return True


def _blk(content, digest=None):
    return {"kind": "block", "content": content,
            "digest": digest or digest_of(content),
            "partition": "P", "protection": 0, "size": 1}


def build_ce_midway_digest(case):
    """反例一:提交后终态看似合理,中途可见根却引用了错误摘要的块。"""
    bad = "sha256:" + "0" * 64
    ce_case = {
        "case_id": f'{case.get("case_id", "case")}::ce-midway-digest',
        "partitions": [{"id": "P", "protection": 0, "writable": True, "capacity": 64}],
        "manifests": {
            "old": {"roots": {"m": "r0"},
                    "slots": {"r0": {"kind": "index", "children": ["b0"]},
                              "b0": _blk("old-block")}},
            "new": {"roots": {"m": "r1"},
                    "slots": {"r1": {"kind": "index", "children": ["b1"]},
                              "b1": _blk("candidate-block")}},
            "fix": {"roots": {"m": "r2"},
                    "slots": {"r2": {"kind": "index", "children": ["b2"]},
                              "b2": _blk("candidate-block")}},
        },
        "trace": [
            {"id": "c1", "op": "stage", "slot": "b1", "partition": "P", "digest": bad},
            {"id": "c2", "op": "commit_root", "index": "m", "to": "new"},
            {"id": "c3", "op": "stage", "slot": "b2", "partition": "P",
             "version": "fix"},
            {"id": "c4", "op": "commit_root", "index": "m", "to": "fix"},
            {"id": "c5", "op": "release", "slot": "b1"},
        ],
    }
    report = run_check(ce_case)
    return {
        "name": "midway-digest",
        "case": ce_case,
        "failing_step": report["first_unsafe"]["step"],
        "reason": report["first_unsafe"]["reason"],
        "final_state_looks_valid": _final_state_digest_ok(ce_case),
        "explanation": "终态所有库存块摘要与内容一致,看似合理;但 c1 写入了错误摘要的"
                       "候选块,c2 提交后到 c4 之间可见根引用了错误摘要。",
    }


def build_ce_shared_old(case):
    """反例二:两次原子提交共享旧块,提前释放导致回滚失败。"""
    ce_case = {
        "case_id": f'{case.get("case_id", "case")}::ce-shared-old-early-release',
        "partitions": [{"id": "P", "protection": 0, "writable": True, "capacity": 64}],
        "manifests": {
            "old": {"roots": {"i1": "rA", "i2": "rB"},
                    "slots": {"rA": {"kind": "index", "children": ["X", "A1"]},
                              "rB": {"kind": "index", "children": ["X", "B1"]},
                              "X": _blk("shared-X"), "A1": _blk("a1"), "B1": _blk("b1")}},
            "new": {"roots": {"i1": "rA2", "i2": "rB2"},
                    "slots": {"rA2": {"kind": "index", "children": ["X2"]},
                              "rB2": {"kind": "index", "children": ["X3"]},
                              "X2": _blk("x2"), "X3": _blk("x3")}},
        },
        "trace": [
            {"id": "s1", "op": "stage", "slot": "X2", "partition": "P"},
            {"id": "c1", "op": "commit_root", "index": "i1", "to": "new"},
            {"id": "r1", "op": "release", "slot": "X"},
            {"id": "s2", "op": "stage", "slot": "X3", "partition": "P"},
            {"id": "c2", "op": "commit_root", "index": "i2", "to": "new"},
        ],
    }
    report = run_check(ce_case)
    step = report["first_unsafe"]["step"]
    eng = _replay_prefix(ce_case, ce_case["trace"], step)
    missing = sorted(s for s in eng.live_refs()
                     if s not in eng.stored and _kind_of(ce_case, s) == "block")
    return {
        "name": "shared-old-early-release",
        "case": ce_case,
        "failing_step": step,
        "shared_slot": "X",
        "crash_simulation": {"after_step": step,
                             "visible_roots": dict(eng.visible),
                             "missing_blocks": missing,
                             "rollback_failure": bool(missing)},
        "explanation": "两次原子提交共享旧块 X;第一次提交后提前释放 X,若第二次提交前"
                       "断电,回滚到旧版 i2 时 X 已丢失,回滚失败。",
    }


def run_verify(case):
    """verify CASE:复算清单摘要与追踪,并生成两类反例。"""
    digest_audit = []
    for ver, man in case.get("manifests", {}).items():
        for sid, s in block_slots(man).items():
            content = s.get("content")
            if content is not None and s.get("digest") != digest_of(content):
                digest_audit.append({"version": ver, "slot": sid,
                                     "declared": s.get("digest"),
                                     "recomputed": digest_of(content)})
    trace_audit = run_check(case) if case.get("trace") else None
    return {
        "case_id": case.get("case_id"),
        "manifest_digest_audit": digest_audit,
        "manifest_digest_ok": not digest_audit,
        "trace_audit": trace_audit,
        "counterexamples": [build_ce_midway_digest(case), build_ce_shared_old(case)],
    }

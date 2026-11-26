"""生成 fixtures/ 下的合成夹具(摘要由内容真实计算,保证可复算)。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from caskpatch_core import digest_of  # noqa: E402


def blk(content, partition, protection=0, size=1, digest=None):
    return {"kind": "block", "content": content,
            "digest": digest or digest_of(content),
            "partition": partition, "protection": protection, "size": size}


def idx(children, protection=0):
    return {"kind": "index", "children": children, "protection": protection}


# ---- ok_case:安全追踪;含同摘要双槽位(a/a2)展示去重 ----
ok_old = {"roots": {"main": "r"},
          "slots": {"r": idx(["a", "b"]),
                    "a": blk("alpha", "P0"),
                    "b": blk("beta", "P1", protection=1)}}
ok_new = {"roots": {"main": "r2"},
          "slots": {"r2": idx(["a", "a2", "b2"]),
                    "a": blk("alpha", "P0"),
                    "a2": blk("alpha", "P0"),
                    "b2": blk("BETA2", "P1", protection=1)}}
ok_case = {
    "case_id": "隔离档案柜-ok-01",
    "partitions": [{"id": "P0", "protection": 0, "writable": True, "capacity": 10},
                   {"id": "P1", "protection": 1, "writable": True, "capacity": 10}],
    "manifests": {"old": ok_old, "new": ok_new},
    "retained_snapshots": [],
    "trace": [{"id": "t1", "op": "stage", "slot": "b2", "partition": "P1"},
              {"id": "t2", "op": "stage", "slot": "a2", "partition": "P0"},
              {"id": "t3", "op": "commit_root", "index": "main", "to": "new"},
              {"id": "t4", "op": "release", "slot": "b"}],
}

# ---- unsafe_release_case:提交前释放旧块,最早不安全断点在 step 0 ----
unsafe_release_case = {
    "case_id": "隔离档案柜-unsafe-release-01",
    "partitions": ok_case["partitions"],
    "manifests": {"old": ok_old, "new": ok_new},
    "trace": [{"id": "u1", "op": "release", "slot": "b"}],
}

# ---- protection_case:低保护分区暂存高保护域块 ----
protection_case = {
    "case_id": "隔离档案柜-protection-01",
    "partitions": [{"id": "PL", "protection": 0, "writable": True, "capacity": 10},
                   {"id": "PH", "protection": 1, "writable": True, "capacity": 10}],
    "manifests": {
        "old": {"roots": {"main": "r"},
                "slots": {"r": idx(["x"], protection=1), "x": blk("x", "PH")}},
        "new": {"roots": {"main": "r2"},
                "slots": {"r2": idx(["x2"], protection=1), "x2": blk("x2", "PH")}},
    },
    "trace": [{"id": "p1", "op": "stage", "slot": "x2", "partition": "PL"}],
}

# ---- arrange_case:PA 满,迫使一次跨域暂存;i3 被保留快照钉住 ----
arrange_case = {
    "case_id": "隔离档案柜-arrange-01",
    "partitions": [{"id": "PA", "protection": 0, "writable": True, "capacity": 2},
                   {"id": "PB", "protection": 0, "writable": True, "capacity": 4},
                   {"id": "PC", "protection": 1, "writable": True, "capacity": 4}],
    "manifests": {
        "old": {"roots": {"i1": "r1", "i2": "r2", "i3": "r3"},
                "slots": {"r1": idx(["o1"]), "o1": blk("o1", "PA"),
                          "r2": idx(["o2"]), "o2": blk("o2", "PA"),
                          "r3": idx(["o3"], protection=1), "o3": blk("o3", "PC")}},
        "new": {"roots": {"i1": "r1n", "i2": "r2n", "i3": "r3n"},
                "slots": {"r1n": idx(["n1"]), "n1": blk("n1", "PA"),
                          "r2n": idx(["n2"]), "n2": blk("n2", "PA"),
                          "r3n": idx(["n3"], protection=1), "n3": blk("n3", "PC")}},
    },
    "retained_snapshots": [{"version": "old", "index": "i3"}],
    "switches": [{"action_id": "sw_b", "index": "i2", "slot": "n2",
                  "old_slot": "o2", "home": "PA"},
                 {"action_id": "sw_a", "index": "i1", "slot": "n1",
                  "old_slot": "o1", "home": "PA"},
                 {"action_id": "sw_c", "index": "i3", "slot": "n3",
                  "old_slot": "o3", "home": "PC"}],
}

# ---- verify_case:两索引共享旧块 X;含一个声明摘要错误的未引用槽位 ----
verify_case = {
    "case_id": "隔离档案柜-verify-01",
    "partitions": [{"id": "P", "protection": 0, "writable": True, "capacity": 64}],
    "manifests": {
        "old": {"roots": {"i1": "rA", "i2": "rB"},
                "slots": {"rA": idx(["X", "A1"]), "rB": idx(["X", "B1"]),
                          "X": blk("shared-X", "P"), "A1": blk("a1", "P"),
                          "B1": blk("b1", "P")}},
        "new": {"roots": {"i1": "rA2", "i2": "rB2"},
                "slots": {"rA2": idx(["X2"]), "rB2": idx(["X3"]),
                          "X2": blk("x2", "P"), "X3": blk("x3", "P"),
                          "b_bad": blk("tampered", "P",
                                       digest="sha256:" + "0" * 64)}},
    },
    "trace": [{"id": "v1", "op": "stage", "slot": "X2", "partition": "P"},
              {"id": "v2", "op": "commit_root", "index": "i1", "to": "new"},
              {"id": "v3", "op": "stage", "slot": "X3", "partition": "P"},
              {"id": "v4", "op": "commit_root", "index": "i2", "to": "new"},
              {"id": "v5", "op": "release", "slot": "X"},
              {"id": "v6", "op": "release", "slot": "A1"},
              {"id": "v7", "op": "release", "slot": "B1"}],
}

FIXTURES = {
    "ok_case.json": ok_case,
    "unsafe_release_case.json": unsafe_release_case,
    "protection_case.json": protection_case,
    "arrange_case.json": arrange_case,
    "verify_case.json": verify_case,
}


def main():
    out = ROOT / "fixtures"
    out.mkdir(exist_ok=True)
    for name, obj in FIXTURES.items():
        (out / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
        print(f"written fixtures/{name}")


if __name__ == "__main__":
    main()

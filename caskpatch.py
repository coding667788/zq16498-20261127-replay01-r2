#!/usr/bin/env python3
"""caskpatch —— 隔离档案柜磁盘索引修复复核工具(离线,纯标准库,UTF-8 JSON CLI)。"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from caskpatch_core import CaseError, run_arrange, run_check, run_verify  # noqa: E402
from caskpatch_db import LocalDB  # noqa: E402


def emit(obj):
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="caskpatch", description="隔离档案柜磁盘索引修复复核工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="复核追踪:重算可见根/重复计数,定位最早不安全断点")
    p.add_argument("case", help="案例 JSON(内含 trace)")
    p.add_argument("trace", nargs="?", help="可选:外部追踪 JSON,覆盖案例内 trace")

    p = sub.add_parser("arrange", help="为分块切换选择顺序与暂存目标")
    p.add_argument("case")

    p = sub.add_parser("verify", help="复算清单并生成反例")
    p.add_argument("case")

    p = sub.add_parser("db-create", help="从案例创建本地数据库")
    p.add_argument("db")
    p.add_argument("case")

    p = sub.add_parser("commit", help="三阶段提交计划(任一阶段可断电)")
    p.add_argument("db")
    p.add_argument("plan")
    p.add_argument("--pause", action="store_true",
                   help="每阶段打印 PHASE <name> 后等待一行输入(供断电测试)")

    p = sub.add_parser("inspect", help="重开数据库并恢复,输出当前状态")
    p.add_argument("db")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "check":
            case = load_json(args.case)
            trace = load_json(args.trace) if args.trace else None
            rep = run_check(case, trace=trace)
            emit(rep)
            return 3 if rep["status"] != "ok" else 0
        if args.cmd == "arrange":
            rep = run_arrange(load_json(args.case))
            emit(rep)
            return 4 if rep["status"] != "ok" else 0
        if args.cmd == "verify":
            emit(run_verify(load_json(args.case)))
            return 0
        if args.cmd == "db-create":
            LocalDB.create(args.db, load_json(args.case))
            emit({"status": "created", "db": args.db})
            return 0
        if args.cmd == "commit":
            rep = LocalDB(args.db).commit(load_json(args.plan), pause=args.pause)
            emit(rep)
            return 0
        if args.cmd == "inspect":
            emit(LocalDB(args.db).inspect())
            return 0
    except (CaseError, KeyError, ValueError, FileNotFoundError) as exc:
        emit({"error": f"{type(exc).__name__}: {exc}"})
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

"""判环工作量对比自测：多个独立风险区 + 大量不变批次。

构造含多个相互独立的错峰风险单元和大量新旧代价不变设备的分批计划，
分别用优化前的逐批全网判环（app.baseline.run_precheck_baseline）与
优化后的区域增量判环（app.core.run_precheck）执行整轮预检，校验两次
结果（首个危险批次、已切换设备、逐跳证据）完全一致，并比较整轮判环
处理的节点数 / 边数。

用法（仓库根目录）：
    python3 scripts/compare_work.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.baseline import run_precheck_baseline  # noqa: E402
from app.core import collect_nodes, run_precheck  # noqa: E402
from app.models import Link  # noqa: E402


def build_payload(gadgets: int, quiet: int, quiet_batch_size: int, danger: bool) -> dict:
    """多个独立风险区（错峰单元）+ 大量下一跳不变的静默设备。"""
    links: list[Link] = []
    for i in range(gadgets):
        links += [
            Link(a="T", b=f"A{i}", old_cost=10, new_cost=30),
            Link(a="T", b=f"B{i}", old_cost=20, new_cost=20),
            Link(a=f"A{i}", b=f"B{i}", old_cost=1, new_cost=1),
        ]
    prev = "T"
    for j in range(quiet):
        links.append(Link(a=prev, b=f"Q{j}", old_cost=5, new_cost=5))
        prev = f"Q{j}"

    batches: list[list[str]] = []
    qs = [f"Q{j}" for j in range(quiet)]
    for c in range(0, quiet, quiet_batch_size):
        batches.append(qs[c : c + quiet_batch_size])  # 不变批次
    if danger:
        batches.append(["A0", "B0"])  # 同批错峰：首个危险批次
        for i in range(1, gadgets):
            batches.append([f"B{i}"])
        for i in range(1, gadgets):
            batches.append([f"A{i}"])
    else:
        for i in range(gadgets):
            batches.append([f"B{i}"])  # B 先切：安全
        for i in range(gadgets):
            batches.append([f"A{i}"])  # A 后切：安全
    batches.append(["T"])
    return {"target": "T", "links": links, "batches": batches}


def compare(name: str, payload: dict) -> bool:
    nodes = collect_nodes(payload["links"])
    stats_opt: dict = {}
    stats_base: dict = {}
    opt = run_precheck(nodes, payload["links"], payload["target"], payload["batches"], stats=stats_opt)
    base = run_precheck_baseline(
        nodes, payload["links"], payload["target"], payload["batches"], stats=stats_base
    )

    same = opt == base
    n_batches = len(payload["batches"])
    print(f"== 场景 {name}: {len(nodes)} 台设备, {n_batches} 批, 结果一致: {'是' if same else '否'}")
    print(f"   首个危险批次: 基线 {base['batches_checked']} / 优化 {opt['batches_checked']}"
          f"（danger={opt['dangerous'] is not None}）")
    print(f"   判环处理节点数: 基线 {stats_base['nodes_processed']:>6} -> 优化 {stats_opt['nodes_processed']:>5}")
    print(f"   判环处理边数:   基线 {stats_base['edges_processed']:>6} -> 优化 {stats_opt['edges_processed']:>5}")
    print(f"   全网扫描次数:   基线 {stats_base['full_scans']:>6} -> 优化 {stats_opt['full_scans']:>5}")
    print(f"   优化明细: 区域检查 {stats_opt['region_checks']} 次, 整批跳过 {stats_opt['batches_skipped']} 批")
    print()
    return same


def main() -> int:
    ok = True
    ok &= compare("pass（6 个独立风险区 + 60 台不变设备）",
                  build_payload(gadgets=6, quiet=60, quiet_batch_size=5, danger=False))
    ok &= compare("danger（同上，但首个风险单元同批错峰）",
                  build_payload(gadgets=6, quiet=60, quiet_batch_size=5, danger=True))
    ok &= compare("pass（20 个独立风险区 + 200 台不变设备）",
                  build_payload(gadgets=20, quiet=200, quiet_batch_size=10, danger=False))
    if not ok:
        print("失败：优化前后结果不一致", file=sys.stderr)
        return 1
    print("全部场景：优化前后结果（首个危险批次 / 已切换设备 / 逐跳证据）完全一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

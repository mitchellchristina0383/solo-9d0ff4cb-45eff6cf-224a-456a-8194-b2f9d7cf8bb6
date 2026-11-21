"""区域增量判环优化的自测。

覆盖：
- 区域划分（两版等价下一跳并集图的强连通分量）识别；
- 优化后（app.core.run_precheck）与优化前基线
  （app.baseline.run_precheck_baseline）在示例、随机拓扑、多风险区
  场景下结果完全一致（首个危险批次、已切换设备、逐跳证据）；
- 含多个独立风险区和大量不变批次的计划上，整轮判环处理的
  节点数 / 边数显著下降。

运行（仓库根目录）：
    PYTHONPATH=/tmp/pylibs python -m pytest -q
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from fastapi.testclient import TestClient

from app.baseline import run_precheck_baseline
from app.core import (
    build_adjacency,
    collect_nodes,
    dijkstra_distances,
    next_hop_sets,
    plan_regions,
    run_precheck,
)
from app.main import app
from app.models import Link, PrecheckRequest

client = TestClient(app)
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _run_both(payload: dict, stats_opt: dict | None = None, stats_base: dict | None = None):
    req = PrecheckRequest(**payload)
    nodes = collect_nodes(req.links)
    opt = run_precheck(nodes, req.links, req.target, req.batches, stats=stats_opt)
    base = run_precheck_baseline(nodes, req.links, req.target, req.batches, stats=stats_base)
    return opt, base


def _nh(nodes, links):
    old_adj = build_adjacency(nodes, links, "old_cost")
    new_adj = build_adjacency(nodes, links, "new_cost")
    old_nh = next_hop_sets(old_adj, dijkstra_distances(old_adj, "T"))
    new_nh = next_hop_sets(new_adj, dijkstra_distances(new_adj, "T"))
    return old_nh, new_nh


def _multi_region_payload(gadgets: int, quiet: int, quiet_batch_size: int, danger: bool) -> dict:
    """构造含多个独立风险区与大量不变批次的计划。

    每个风险单元 i 是经典错峰结构（A_i 先切会成 A_i->B_i->A_i 环，
    B_i 先切则安全）；静默设备 Q_j 新旧代价相同，下一跳不变。
    """
    links = []
    for i in range(gadgets):
        links += [
            {"a": "T", "b": f"A{i}", "old_cost": 10, "new_cost": 30},
            {"a": "T", "b": f"B{i}", "old_cost": 20, "new_cost": 20},
            {"a": f"A{i}", "b": f"B{i}", "old_cost": 1, "new_cost": 1},
        ]
    prev = "T"
    for j in range(quiet):
        links.append({"a": prev, "b": f"Q{j}", "old_cost": 5, "new_cost": 5})
        prev = f"Q{j}"

    batches: list[list[str]] = []
    qs = [f"Q{j}" for j in range(quiet)]
    for c in range(0, quiet, quiet_batch_size):
        batches.append(qs[c : c + quiet_batch_size])  # 大量不变批次
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


# ---------------------------------------------------------------- 区域识别
def test_plan_regions_identifies_independent_gadgets():
    payload = _multi_region_payload(gadgets=3, quiet=4, quiet_batch_size=2, danger=False)
    req = PrecheckRequest(**payload)
    nodes = collect_nodes(req.links)
    old_nh, new_nh = _nh(nodes, req.links)
    plan = plan_regions(nodes, "T", old_nh, new_nh)
    # 每个错峰风险单元构成一个二元区域；目标与静默设备不在任何区域内
    assert sorted(sorted(r) for r in plan.regions) == [
        ["A0", "B0"],
        ["A1", "B1"],
        ["A2", "B2"],
    ]
    assert "T" not in plan.region_of
    assert all(f"Q{j}" not in plan.region_of for j in range(4))
    # A 在区域内新增了下一跳 B（仅新版），B 没有区域内新增
    rid = plan.region_of["A0"]
    assert plan.added[rid]["A0"] == frozenset({"B0"})
    assert plan.added[rid]["B0"] == frozenset()


def test_plan_regions_ecmp_merges_component():
    """等价路径场景：A/B/C 互相可达，应划入同一可查环区域。"""
    req = PrecheckRequest(
        target="T",
        links=[
            Link(a="T", b="A", old_cost=10, new_cost=100),
            Link(a="T", b="B", old_cost=10, new_cost=100),
            Link(a="T", b="C", old_cost=100, new_cost=10),
            Link(a="A", b="C", old_cost=1, new_cost=1),
            Link(a="B", b="C", old_cost=1, new_cost=1),
        ],
        batches=[["A", "B", "C", "T"]],
    )
    nodes = collect_nodes(req.links)
    old_nh, new_nh = _nh(nodes, req.links)
    plan = plan_regions(nodes, "T", old_nh, new_nh)
    assert sorted(sorted(r) for r in plan.regions) == [["A", "B", "C"]]


# ---------------------------------------------------------------- 等价性：示例
def test_examples_match_baseline():
    for path in sorted(EXAMPLES.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        opt, base = _run_both(payload)
        assert opt == base, path.name


# ---------------------------------------------------------------- 等价性：随机拓扑
def test_random_topologies_match_baseline():
    rng = random.Random(20261121)
    node_names = [f"n{i}" for i in range(8)]
    for trial in range(120):
        edges: list[tuple[int, int]] = []
        seen_pairs: set[frozenset[int]] = set()
        order = list(range(1, 8))
        rng.shuffle(order)
        connected = {0}
        for v in order:
            u = rng.choice(tuple(connected))
            edges.append((u, v))
            seen_pairs.add(frozenset((u, v)))
            connected.add(v)
        for _ in range(rng.randint(0, 6)):
            u, v = rng.sample(range(8), 2)
            key = frozenset((u, v))
            if key not in seen_pairs:
                seen_pairs.add(key)
                edges.append((u, v))
        links = [
            Link(
                a=node_names[u],
                b=node_names[v],
                old_cost=rng.randint(1, 9),
                new_cost=rng.randint(1, 9),
            )
            for u, v in edges
        ]
        # 随机划分 1~4 个非空批次，覆盖全部节点
        rest = node_names[:]
        rng.shuffle(rest)
        k = rng.randint(1, len(rest))
        cuts = sorted(rng.sample(range(1, len(rest)), k - 1)) if k > 1 else []
        batches, prev_b = [], 0
        for c in cuts + [len(rest)]:
            batches.append(rest[prev_b:c])
            prev_b = c

        nodes = set(node_names)
        stats_opt: dict = {}
        opt = run_precheck(nodes, links, "n0", batches, stats=stats_opt)
        base = run_precheck_baseline(nodes, links, "n0", batches)
        assert opt == base, trial
        # 优化后判环处理的图规模不超过基线
        assert stats_opt["nodes_processed"] <= (len(nodes) * len(batches))


# ---------------------------------------------------------------- 工作量对比：多风险区 + 大量不变批次
def test_multi_region_pass_workload_savings():
    payload = _multi_region_payload(gadgets=6, quiet=60, quiet_batch_size=5, danger=False)
    stats_opt: dict = {}
    stats_base: dict = {}
    opt, base = _run_both(payload, stats_opt, stats_base)

    assert opt == base
    assert opt["dangerous"] is None
    assert opt["batches_checked"] == len(payload["batches"]) == 25

    # 19 个批次（12 个静默批 + 6 个 B 批 + 目标批）无需任何判环，
    # 仅 6 个 A 批各自触发一次二元区域检查
    assert stats_opt["batches_skipped"] == 19
    assert stats_opt["region_checks"] == 6
    assert stats_opt["full_scans"] == 0
    assert stats_base["full_scans"] == 25

    # 整轮判环处理的节点数 / 边数大幅下降
    assert stats_opt["nodes_processed"] * 20 < stats_base["nodes_processed"]
    assert stats_opt["edges_processed"] * 20 < stats_base["edges_processed"]
    print(
        "\n[pass] 判环处理 节点: 基线 {bn} -> 优化 {on}；边: 基线 {be} -> 优化 {oe}".format(
            bn=stats_base["nodes_processed"],
            on=stats_opt["nodes_processed"],
            be=stats_base["edges_processed"],
            oe=stats_opt["edges_processed"],
        )
    )


def test_multi_region_danger_evidence_identical():
    payload = _multi_region_payload(gadgets=6, quiet=60, quiet_batch_size=5, danger=True)
    stats_opt: dict = {}
    stats_base: dict = {}
    opt, base = _run_both(payload, stats_opt, stats_base)

    # 首个危险批次、已切换设备、逐跳证据与基线完全一致
    assert opt == base
    danger = opt["dangerous"]
    assert danger is not None
    assert danger["batch_index"] == 13  # 12 个静默批次之后的首个风险批次
    assert opt["batches_checked"] == 13
    assert danger["switched_devices"] == ["A0"]
    assert danger["cycle_routers"] == ["A0", "B0", "A0"]
    hop_pairs = [(h["router"], h["next_hop"], h["via"]) for h in danger["forwarding_cycle"]]
    assert ("A0", "B0", "new") in hop_pairs
    assert ("B0", "A0", "old") in hop_pairs

    # 危险批次之前的 12 个不变批次全部跳过；仅一次区域检查 + 一次全网取证
    assert stats_opt["batches_skipped"] == 12
    assert stats_opt["region_checks"] == 1
    assert stats_opt["full_scans"] == 1
    assert stats_base["full_scans"] == 13
    assert stats_opt["nodes_processed"] < stats_base["nodes_processed"]
    print(
        "\n[danger] 判环处理 节点: 基线 {bn} -> 优化 {on}；边: 基线 {be} -> 优化 {oe}".format(
            bn=stats_base["nodes_processed"],
            on=stats_opt["nodes_processed"],
            be=stats_base["edges_processed"],
            oe=stats_opt["edges_processed"],
        )
    )


def test_multi_region_via_api():
    payload = _multi_region_payload(gadgets=2, quiet=6, quiet_batch_size=3, danger=False)
    r = client.post("/precheck", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "pass"
    assert data["batches_checked"] == data["batches_total"]
    req = PrecheckRequest(**payload)
    nodes = collect_nodes(req.links)
    expect = run_precheck(nodes, req.links, req.target, req.batches)
    assert data["old_next_hops"] == expect["old_nh"]
    assert data["new_next_hops"] == expect["new_nh"]

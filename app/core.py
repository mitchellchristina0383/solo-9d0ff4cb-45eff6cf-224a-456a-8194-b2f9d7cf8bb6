"""核心算法：Dijkstra 下一跳计算、区域划分与增量成环检测。

语义约定
--------
- 拓扑为无向连通图，每条链路双向同代价（旧代价 / 新代价各一版）。
- 某台路由器在某一版代价下，到目标代价相等的所有下一跳都可能被选中
  （等价多路径，ECMP），因此下一跳是一个集合。
- 批次 k 进行过程中：
    * 更早批次的路由器已全部使用新版下一跳；
    * 更晚批次的路由器仍使用旧版下一跳；
    * 本批路由器可处于旧版或新版状态，因此其可用出边为两版下一跳的并集。
- 上述并集转发图中若存在有向环，则存在一种错峰切换顺序 / 下一跳选择使流量
  兜圈；反之并集图无环时，本批切换过程中的任何具体状态都不可能成环。

区域增量判环
------------
逐批对全网判环代价高昂：许多批次只改变少数设备的下一跳。优化按
「两版全部等价下一跳」识别可独立查环的区域：

- 令 G* 为每节点出边取「旧版 ∪ 新版下一跳」的有向图。任意批次的并集转发图
  都是 G* 的子图，因此其中任何有向环都完整落在 G* 的某个强连通分量（SCC）
  内；单节点分量没有自环（代价为正，下一跳上 dist 严格递减），永不需检查。
- 批次 k 相对批次 k-1，区域内子图只有两类变化：上一批节点「旧∪新 → 仅新」
  是纯删边；本批节点「仅旧 → 旧∪新」只可能新增 new−old 的边。删边不会
  产生新环，故第 k 批只需重查「本批设备在该区域内新增了下一跳」的区域。
- 其余区域沿用已有安全结论：从未被触及的区域其子图始终是旧版下一跳图的
  子图（旧版图按 dist 严格递减，是 DAG）；曾被检查过的区域自上次检查以来
  只有删边或无变化，结论保持安全。

首个危险批次的判定与逐批全网判环严格等价；命中危险时在该批的全网并集图上
重跑一次相同的确定性找环，保证首个危险批次、已切换设备与逐跳证据和优化前
完全一致。
"""

from __future__ import annotations

import heapq
from typing import Iterable, NamedTuple

from .models import Link

INF = float("inf")


def collect_nodes(links: Iterable[Link]) -> set[str]:
    nodes: set[str] = set()
    for lk in links:
        nodes.add(lk.a)
        nodes.add(lk.b)
    return nodes


def build_adjacency(nodes: set[str], links: Iterable[Link], attr: str) -> dict[str, dict[str, int]]:
    """构造无向邻接表，attr 取 'old_cost' 或 'new_cost'。"""
    adj: dict[str, dict[str, int]] = {n: {} for n in nodes}
    for lk in links:
        weight = getattr(lk, attr)
        adj[lk.a][lk.b] = weight
        adj[lk.b][lk.a] = weight
    return adj


def dijkstra_distances(adj: dict[str, dict[str, int]], source: str) -> dict[str, float]:
    dist: dict[str, float] = {n: INF for n in adj}
    dist[source] = 0
    pq: list[tuple[int, str]] = [(0, source)]
    while pq:
        d, u = heapq.heappop(pq)
        if d != dist[u]:
            continue
        for v, w in adj[u].items():
            nd = d + w
            if nd < dist[v]:
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    return dist


def next_hop_sets(
    adj: dict[str, dict[str, int]], dist: dict[str, float]
) -> dict[str, set[str]]:
    """依据最短距离推导每台路由器的全部等价下一跳。

    对无向边 (u, v, w)，v 是 u 的下一跳当且仅当 dist[u] == w + dist[v]。
    目标路由器自身没有下一跳。
    """
    nh: dict[str, set[str]] = {n: set() for n in adj}
    for u in adj:
        for v, w in adj[u].items():
            if dist[u] == w + dist[v]:
                nh[u].add(v)
    return nh


def is_connected(nodes: set[str], links: Iterable[Link], source: str) -> bool:
    adj: dict[str, list[str]] = {n: [] for n in nodes}
    for lk in links:
        adj[lk.a].append(lk.b)
        adj[lk.b].append(lk.a)
    seen = {source}
    stack = [source]
    while stack:
        u = stack.pop()
        for v in adj[u]:
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return seen == nodes


def find_cycle(adj: dict[str, Iterable[str]]) -> list[str] | None:
    """迭代式三色 DFS，返回有向环的节点序列（首尾相同），无环返回 None。

    邻接表按字典序遍历，保证结果确定。
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in adj}
    for start in sorted(adj):
        if color[start] != WHITE:
            continue
        color[start] = GRAY
        path = [start]
        stack: list[tuple[str, Iterable[str]]] = [(start, iter(sorted(adj[start])))]
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                color[node] = BLACK
                stack.pop()
                path.pop()
                continue
            if color[nxt] == GRAY:
                idx = path.index(nxt)
                return path[idx:] + [nxt]
            if color[nxt] == WHITE:
                color[nxt] = GRAY
                path.append(nxt)
                stack.append((nxt, iter(sorted(adj[nxt]))))
    return None


def strongly_connected_components(adj: dict[str, Iterable[str]]) -> list[list[str]]:
    """Kosaraju 强连通分量（迭代实现，遍历顺序确定，结果确定）。"""
    visited: set[str] = set()
    finish: list[str] = []
    for start in sorted(adj):
        if start in visited:
            continue
        visited.add(start)
        stack: list[tuple[str, Iterable[str]]] = [(start, iter(sorted(adj[start])))]
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                finish.append(node)
                stack.pop()
                continue
            if nxt not in visited:
                visited.add(nxt)
                stack.append((nxt, iter(sorted(adj[nxt]))))

    radj: dict[str, set[str]] = {n: set() for n in adj}
    for u, outs in adj.items():
        for v in outs:
            radj[v].add(u)

    comps: list[list[str]] = []
    seen: set[str] = set()
    for start in reversed(finish):
        if start in seen:
            continue
        seen.add(start)
        comp: list[str] = []
        stack = [start]
        while stack:
            u = stack.pop()
            comp.append(u)
            for v in sorted(radj[u], reverse=True):
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        comps.append(comp)
    return comps


class RegionPlan(NamedTuple):
    """可独立查环的区域划分（基于两版等价下一跳并集图的强连通分量）。"""

    regions: list[list[str]]  # 每个区域的节点（字典序），仅含可能成环的区域
    region_of: dict[str, int]  # 节点 -> 区域 id；不在任何区域的节点缺席
    old_edges: list[dict[str, frozenset[str]]]  # 各区域内旧版下一跳
    new_edges: list[dict[str, frozenset[str]]]  # 各区域内新版下一跳
    added: list[dict[str, frozenset[str]]]  # 各区域内新增下一跳（new − old）


def plan_regions(
    nodes: set[str],
    target: str,
    old_nh: dict[str, set[str]],
    new_nh: dict[str, set[str]],
) -> RegionPlan:
    """按两版全部等价下一跳识别可独立查环的区域。

    任何批次的并集转发图都是 G*（每节点出边 = 旧 ∪ 新下一跳）的子图，
    环必完整落在 G* 的某个强连通分量内；无自环的单节点分量永不需检查。
    """
    union_all: dict[str, set[str]] = {n: set() for n in nodes}
    for n in nodes:
        if n == target:
            continue
        union_all[n] = old_nh[n] | new_nh[n]

    regions: list[list[str]] = []
    region_of: dict[str, int] = {}
    old_edges: list[dict[str, frozenset[str]]] = []
    new_edges: list[dict[str, frozenset[str]]] = []
    added: list[dict[str, frozenset[str]]] = []
    for comp in strongly_connected_components(union_all):
        if len(comp) == 1 and comp[0] not in union_all[comp[0]]:
            continue  # 单节点且无自环：任何批次状态下都不可能成环
        rid = len(regions)
        members = sorted(comp)
        member_set = set(members)
        old_e: dict[str, frozenset[str]] = {}
        new_e: dict[str, frozenset[str]] = {}
        add_e: dict[str, frozenset[str]] = {}
        for n in members:
            region_of[n] = rid
            o = frozenset(old_nh[n] & member_set)
            w = frozenset(new_nh[n] & member_set)
            old_e[n] = o
            new_e[n] = w
            add_e[n] = w - o
        regions.append(members)
        old_edges.append(old_e)
        new_edges.append(new_e)
        added.append(add_e)
    return RegionPlan(regions, region_of, old_edges, new_edges, added)


def union_adj_at_batch(
    nodes: set[str],
    target: str,
    batch_of: dict[str, int],
    batch_set: set[str],
    k: int,
    old_nh: dict[str, set[str]],
    new_nh: dict[str, set[str]],
) -> dict[str, set[str]]:
    """批次 k 切换期间的全网并集转发图（前批仅新、后批仅旧、本批取并集）。"""
    union_adj: dict[str, set[str]] = {n: set() for n in nodes}
    for n in nodes:
        if n == target:
            continue
        if batch_of[n] < k:
            union_adj[n] = set(new_nh[n])
        elif batch_of[n] > k:
            union_adj[n] = set(old_nh[n])
        elif n in batch_set:
            union_adj[n] = old_nh[n] | new_nh[n]
    return union_adj


def _region_adj_at_batch(
    plan: RegionPlan, rid: int, batch_of: dict[str, int], k: int
) -> dict[str, Iterable[str]]:
    """批次 k 时区域 rid 的诱导子图（仅区域内边，跨区域的边不可能在环上）。"""
    old_e = plan.old_edges[rid]
    new_e = plan.new_edges[rid]
    adj: dict[str, Iterable[str]] = {}
    for n in plan.regions[rid]:
        b = batch_of[n]
        if b < k:
            adj[n] = new_e[n]
        elif b > k:
            adj[n] = old_e[n]
        else:
            adj[n] = old_e[n] | new_e[n]
    return adj


def run_precheck(
    nodes: set[str],
    links: list[Link],
    target: str,
    batches: list[list[str]],
    stats: dict[str, int] | None = None,
) -> dict:
    """执行完整预检，返回结构化结果字典。

    结果与逐批全网判环的原始实现（app.baseline.run_precheck_baseline）
    完全一致；内部按区域增量判环，每批只重查转发有变化的区域。
    stats 若提供，将累计判环处理的图规模（节点数 / 边数）等工作量计数。
    """
    old_adj = build_adjacency(nodes, links, "old_cost")
    new_adj = build_adjacency(nodes, links, "new_cost")

    old_dist = dijkstra_distances(old_adj, target)
    new_dist = dijkstra_distances(new_adj, target)
    old_nh = next_hop_sets(old_adj, old_dist)
    new_nh = next_hop_sets(new_adj, new_dist)

    batch_of: dict[str, int] = {}
    for idx, batch in enumerate(batches):
        for router in batch:
            batch_of[router] = idx

    plan = plan_regions(nodes, target, old_nh, new_nh)

    if stats is not None:
        stats["nodes_processed"] = 0
        stats["edges_processed"] = 0
        stats["region_checks"] = 0
        stats["batches_skipped"] = 0
        stats["full_scans"] = 0

    def _count(adj: dict[str, Iterable[str]]) -> None:
        if stats is not None:
            stats["nodes_processed"] += len(adj)
            stats["edges_processed"] += sum(len(outs) for outs in adj.values())

    dangerous: dict | None = None
    for k, batch in enumerate(batches):
        # 本批设备在哪些区域内新增了下一跳（new − old，仅计区域内边）；
        # 其余区域相对上批只有删边或无变化，沿用已有安全结论。
        affected: set[int] = set()
        for n in batch:
            rid = plan.region_of.get(n)
            if rid is not None and plan.added[rid][n]:
                affected.add(rid)

        if not affected:
            if stats is not None:
                stats["batches_skipped"] += 1
            continue

        hit = False
        for rid in sorted(affected):
            region_adj = _region_adj_at_batch(plan, rid, batch_of, k)
            _count(region_adj)
            if stats is not None:
                stats["region_checks"] += 1
            if find_cycle(region_adj) is not None:
                hit = True
                break
        if not hit:
            continue

        # 首个危险批次：在该批全网并集图上重跑相同的确定性找环，
        # 保证环、已切换设备与逐跳证据和逐批全网判环完全一致。
        union_adj = union_adj_at_batch(nodes, target, batch_of, set(batch), k, old_nh, new_nh)
        _count(union_adj)
        if stats is not None:
            stats["full_scans"] += 1
        cycle = find_cycle(union_adj)
        if cycle is None:  # 区域内有环则全网必有环，此处不可能触发
            raise RuntimeError("区域判环与全网判环结果不一致")
        hops: list[dict] = []
        switched: list[str] = []
        for u, v in zip(cycle, cycle[1:]):
            in_old = v in old_nh[u]
            in_new = v in new_nh[u]
            via = "both" if in_old and in_new else ("new" if in_new else "old")
            hops.append({"router": u, "next_hop": v, "via": via})
            # 本批路由器只有在该出边仅存在于新版下一跳时才必须已切换；
            # 属于旧版（或两版皆有）的边无需切换即可采用。
            if batch_of[u] == k and not in_old:
                switched.append(u)
        dangerous = {
            "batch_index": k + 1,
            "switched_devices": sorted(set(switched)),
            "cycle_routers": cycle,
            "forwarding_cycle": hops,
        }
        break

    return {
        "old_nh": {n: sorted(old_nh[n]) for n in sorted(nodes)},
        "new_nh": {n: sorted(new_nh[n]) for n in sorted(nodes)},
        "dangerous": dangerous,
        "batches_checked": len(batches) if dangerous is None else dangerous["batch_index"],
    }

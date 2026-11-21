"""优化前的逐批全网判环实现（基线）。

仅用于回归对照与判环工作量对比：优化后的 ``app.core.run_precheck``
（区域增量判环）必须在所有输入上给出与本实现完全一致的结果——
相同的首个危险批次、已切换设备与逐跳证据。服务运行路径不依赖本模块。
"""

from __future__ import annotations

from .core import (
    build_adjacency,
    dijkstra_distances,
    find_cycle,
    next_hop_sets,
    union_adj_at_batch,
)
from .models import Link


def run_precheck_baseline(
    nodes: set[str],
    links: list[Link],
    target: str,
    batches: list[list[str]],
    stats: dict[str, int] | None = None,
) -> dict:
    """逐批对全网并集转发图判环（优化前实现），返回结构化结果字典。

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

    if stats is not None:
        stats["nodes_processed"] = 0
        stats["edges_processed"] = 0
        stats["full_scans"] = 0

    dangerous: dict | None = None
    for k, batch in enumerate(batches):
        union_adj = union_adj_at_batch(nodes, target, batch_of, set(batch), k, old_nh, new_nh)
        if stats is not None:
            stats["nodes_processed"] += len(union_adj)
            stats["edges_processed"] += sum(len(outs) for outs in union_adj.values())
            stats["full_scans"] += 1

        cycle = find_cycle(union_adj)
        if cycle is not None:
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

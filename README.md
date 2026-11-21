# 链路代价分批调整离线预检

机房分批调整链路代价前的**离线环路预检服务**。提交连通路由拓扑、每条双向
链路的新旧两版正整数代价、目标路由器和覆盖全部路由器的分批计划，服务按
两版代价分别计算到目标的最短路径下一跳（等价路径的所有下一跳都可能被选
中），逐批判断错峰切换过程中是否可能出现转发环路。

## 判环模型

对每一批 `k`，切换期间各路由器的可用出边为：

| 路由器所属 | 切换期间可用出边 |
| --- | --- |
| 早于第 `k` 批 | 仅新版下一跳（前一批已全部生效） |
| 晚于第 `k` 批 | 仅旧版下一跳 |
| 第 `k` 批（本批） | **旧版 ∪ 新版**下一跳（设备可按任意顺序切换） |

在该并集转发图中：

- **存在有向环** ⇒ 存在一种错峰切换顺序与下一跳选择使流量兜圈，本批危险；
  环上仅新版可达的出边对应的本批设备，即为构造该环所需的“已切换设备”。
- **不存在有向环** ⇒ 本批切换的任意中间状态（任意已切换子集、任意等价
  下一跳选择）都不可能成环。

该等价关系由随机小拓扑暴力枚举（枚举全部切换子集 × 全部下一跳组合）
回归测试守护，见 `tests/test_api.py::test_union_lemma_against_bruteforce`。

下一跳由 Dijkstra 最短距离推导：无向边 `(u, v, w)` 上，`v` 是 `u` 的下一跳
当且仅当 `dist(u) == w + dist(v)`，等号成立的邻居全部纳入等价下一跳集合。

## 区域增量判环

许多批次只改变少数设备的下一跳，逐批对**全网**判环浪费明显。优化按
「两版全部等价下一跳」识别可独立查环的区域，每批只重查转发有变化的区域：

- **区域划分**：令 G* 为每台路由器出边取「旧版 ∪ 新版下一跳」的有向图。
  任意批次的并集转发图都是 G* 的子图，因此任何有向环都完整落在 G* 的
  某个强连通分量内——每个多节点分量就是一个可独立查环的区域
  （`app.core.plan_regions`，Kosaraju 求 SCC）。单节点分量没有自环
  （代价为正，下一跳上 dist 严格递减），永不需检查。
- **增量检查**：批次 k 相对批次 k-1，区域内子图只有两类变化——上一批
  设备「旧∪新 → 仅新」是纯删边；本批设备「仅旧 → 旧∪新」只可能新增
  new−old 的边。删边不会产生新环，故第 k 批只重查「本批设备在该区域内
  新增了下一跳」的区域；其余区域沿用已有安全结论（从未被触及的区域其
  子图始终是旧版下一跳 DAG 的子图；被查过的区域自上次检查以来只有删边
  或无变化）。
- **响应一致**：首个危险批次的判定与逐批全网判环严格等价；命中危险时
  在该批的全网并集图上重跑一次相同的确定性找环，首个危险批次、已切换
  设备与逐跳证据与优化前完全一致。优化前的实现保留在 `app/baseline.py`，
  由 `tests/test_incremental.py` 与 `scripts/compare_work.py` 做对照。

## 目录结构

```
app/
  main.py      FastAPI 入口（/、/health、/precheck、/docs）
  core.py      Dijkstra、下一跳集合、区域划分（SCC）与区域增量判环
  baseline.py  优化前的逐批全网判环实现（仅回归对照 / 工作量对比用）
  models.py    请求/响应模型与输入校验
tests/         pytest 自测（含同批错峰、ECMP、跨批次、暴力对照、区域增量对照）
scripts/
  compare_work.py  判环工作量对比自测（多独立风险区 + 大量不变批次）
examples/      可直接提交的示例请求（含多风险区 multi_region.json）
Dockerfile
docker-compose.yml
requirements.txt
```

## 启动（Docker Compose）

```bash
docker compose up --build
```

- 交互式文档：<http://localhost:8000/docs> （在 `/docs` 的 `POST /precheck`
  上 "Try it out" 提交计划）
- 健康检查：`GET http://localhost:8000/health`

无 Docker 时本地运行：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 请求格式

```json
{
  "target": "T",
  "links": [
    {"a": "T", "b": "A", "old_cost": 10, "new_cost": 30},
    {"a": "T", "b": "B", "old_cost": 20, "new_cost": 20},
    {"a": "A", "b": "B", "old_cost": 1, "new_cost": 1}
  ],
  "batches": [["A", "B", "T"]]
}
```

约束（违反返回 HTTP 422）：

- 代价为正整数；不允许自环、重复链路；拓扑必须连通。
- 分批计划覆盖所有路由器且不重不漏；单批最多 **50** 台；不允许空批次。
- 批次顺序即切换顺序，前一批全部生效后才进入下一批。

## 响应

安全：

```json
{"status": "pass", "target": "T", "batches_total": 3, "batches_checked": 3,
 "old_next_hops": {...}, "new_next_hops": {...}, "danger": null}
```

危险（首个危险批次）：

```json
{
  "status": "danger",
  "batches_checked": 1,
  "danger": {
    "batch_index": 1,
    "switched_devices": ["A"],
    "cycle_routers": ["A", "B", "A"],
    "forwarding_cycle": [
      {"router": "A", "next_hop": "B", "via": "new"},
      {"router": "B", "next_hop": "A", "via": "old"}
    ]
  }
}
```

`via` 标出该跳取自哪一版下一跳：`old` / `new` / `both`。

## 自测

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -q
```

覆盖：同批错峰成环、等价路径并集成环、安全多批次、第二批才成环、
单批 50 台上限等输入校验、断链拒绝、单路由器拓扑、结果确定性，
40 组随机拓扑的并集判环 vs 全状态暴力枚举对照，以及区域增量判环
相对基线实现的等价性与工作量对比（120 组随机拓扑 + 多风险区场景）。

判环工作量对比（多个独立风险区 + 大量不变批次，比较优化前后整轮
判环处理的节点数 / 边数）：

```bash
python scripts/compare_work.py
```

## 复杂度

预处理：两次 Dijkstra `O((V+E) log V)` + 并集图 SCC 区域划分 `O(V+E*)`
（E* 为两版下一跳并集边数，不超过 2E）。判环：设第 k 批触及的区域集合
为 Aff_k，整轮判环处理量为 `Σ_k Σ_{R∈Aff_k} (|V_R|+|E_R|)`——不变批次与
未触及区域零开销；首个危险批次额外一次全网找环 `O(V+E)` 用于生成与
优化前一致的证据。最坏情况（全部设备同属一个区域）退化为优化前的
`O((V+E) log V + B(V+E))`。

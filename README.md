# RF Intermodulation Analysis API（射频互调分析 API）

基于 **FastAPI + SQLite** 的射频三阶互调（`2·fa − fb`）分析服务。

创建站点（频率方案）并录入整数 Hz 表示的载频、扫描范围、保护频段与保护间隔；
服务端枚举不同载频组合的三阶互调产物，过滤扫描范围外的结果，对同频产物聚合全部来源组合，
并计算每个产物到载频或保护频段的最短距离。分析结果按频率稳定排序并持久化，可随时查询。

## 快速开始（单条命令）

```bash
docker compose up --build
```

启动后：

- **API 地址**：`http://localhost:8000`
- **交互式文档（Swagger UI）**：`http://localhost:8000/docs`
- **OpenAPI JSON**：`http://localhost:8000/openapi.json`
- **端口**：`8000`（宿主机 `8000` → 容器 `8000`，可在 `docker-compose.yml` 中修改）

数据库文件在容器启动时自动初始化（幂等建表，含 `idempotency_keys` 幂等登记表），无需手动迁移；
数据持久化在命名卷 `imd-data`（容器内路径 `/data/intermod.db`）中，运维备份默认发布到同卷的
`/data/backups/` 目录（见[运维备份](#运维备份maintenance-backups)）。
旧版本已存在的数据库在下次启动时会自动补建幂等表（`CREATE TABLE IF NOT EXISTS`），不影响既有数据。

停止并清理：`docker compose down`（加 `-v` 同时删除数据卷）。

## 分析语义

- **产物枚举**：对全部有序载频对 `(fa, fb)` 且 `fa ≠ fb` 计算 `2·fa − fb`。
- **范围过滤**：仅保留落在 `[scan_start_hz, scan_end_hz]`（闭区间）内的产物。
- **同频聚合**：同一产物频率的全部来源组合 `(fa, fb)` 聚合到一条结果中，按 `(fa, fb)` 升序排列。
- **最短距离与命中对象**：
  - 到载频的距离：`|f − fc|`；
  - 到保护频段的距离：落在带内为 `0`，否则为到最近边界的距离；
  - 取二者中的最小值作为 `distance_hz`，对应对象即 `hit`（`carrier` 或 `guard_band`）；
  - 距离相同时按确定性规则决胜：载频优先于保护频段，其次按参考频率升序。
- **保护间隔**：`violates_guard_interval = (distance_hz < guard_interval_hz)`，
  即产物到命中对象的间距小于保护间隔时判定为违规。
- **排序**：结果按产物频率升序；同频产物已聚合，频率唯一，排序稳定确定。

## 风险分组（risk groups）

`GET /analyses/{analysis_id}/risk-groups` 仅基于**已持久化**的分析结果计算，不重新扫描：

- **风险节点**：每个 `violates_guard_interval = true` 的违规产物是一个节点；
  非违规产物不参与（不会把不同簇桥接起来）。
- **连边**：节点的全部来源组合 `(fa_hz, fb_hz)` 中的载频都与该节点相连，
  构成"产物 ↔ 载频"二部图；同频产物的多来源已聚合在同一节点。
- **归组**：二部图的每个连通分量即一个风险组——共享载频的产物沿链式边传递归组。
- **每组字段**：`carriers_hz`（升序）、`product_freqs_hz`（升序）、
  `source_pair_count`（组内来源组合总数）、`distance_hz`（组内最小距离）。
- **稳定编号**：组按 `(最小载频, 最小产物频率)` 升序排序后，`group_id` 从 1 连续编号；
  各组载频集合互不相交，排序键唯一，编号稳定可复现。
- 分析不存在 → `404`；无违规产物 → `200` 返回空列表 `[]`。

## 停用方案（mitigation plan）

`GET /analyses/{analysis_id}/mitigation-plan?max_disabled=k` 仅基于**已持久化**的分析结果计算，不重新扫描。
查询参数 `max_disabled` 为 **1～8 的整数**（缺失、越界或非整数 → `422`）。

- **消除判定**：把每个违规产物的每个来源组合 `(fa_hz, fb_hz)` 看作连接载频 `fa_hz`、`fb_hz`
  的一条无向边；一个违规产物被消除，当且仅当它的**全部**来源边都至少有一个端点载频被停用。
  因此停用集合必须是全部违规来源边的**点覆盖**（vertex cover）。
- **精确最小解**：服务端在停用数不超过 `max_disabled` 的前提下求**基数最小**的点覆盖
  （FPT 分支限界，按"选一条未覆盖边 → 停用其两个端点之一"递归，上限 8 时至多
  2⁸ 个叶节点，配极大匹配下界剪枝，保证精确而非贪心近似）；迭代加深先保证停用数最少。
- **字典序择优**：存在多个基数相同的最小解时，按停用载频**升序列表的字典序**取最小者
  （例如 `[100, 400]` 优先于 `[200, 400]`）。结果恒为升序，重复查询稳定可复现。
- **可行响应**：`feasible=true`，`disabled_carriers_hz` 为最小停用集合，
  `eliminated_product_freqs_hz` 为全部违规产物频率（升序），`remaining_product_freqs_hz` 恒为 `[]`。
- **上限内无解**：`feasible=false`。此时返回上限内能**消除违规产物最多**的尽力方案
  （并列时覆盖的来源组合数更多、再并列时停用数更少、最后按升序列表字典序择优），
  `disabled_carriers_hz` 给出该方案，`remaining_product_freqs_hz` 列出仍未消除的产物频率（升序）。
  提高 `max_disabled` 后可能变为可行；可行解的停用数只取决于数据，不会因放宽上限而多停载频。
- 分析不存在 → `404`；无违规产物 → 任意上限都返回 `feasible=true` 与空集合。

## 现场复测扫描窗口（scan windows）

`GET /analyses/{analysis_id}/scan-windows?width_hz=w&max_windows=k` 基于**已保存分析**的违规产物
频率规划现场复测，**不重新扫描**。查询参数 `width_hz` 为**正整数**、`max_windows` 为 **1～3 的整数**
（缺失、非整数或越界 → `422`；分析不存在 → `404`）。

- **窗口定义**：每个窗口是站点扫描范围 `[scan_start_hz, scan_end_hz]` 内长度恰为 `width_hz` 个
  整数 Hz 点的闭区间 `[start_hz, end_hz]`，即 `end_hz - start_hz + 1 = width_hz`；窗口之间
  **互不重叠**（后一窗口起点严格大于前一窗口终点）。
- **非法宽度**：`width_hz` 超过扫描范围可容纳的整数 Hz 点数
  `scan_end_hz - scan_start_hz + 1` 时返回 `422`。
- **优化目标**：选择至多 `max_windows` 个窗口，使覆盖的**不同违规产物频率数最大**（窗口命中某违规
  频率即该频率落在闭区间内；非违规产物不参与）。并列时：
  1. 先取**窗口数更少**的方案（能用一个窗口覆盖同样多频率时不返回两个）；
  2. 再取窗口**起点升序列表字典序最小**的方案（如起点列表 `[1, 301]` 优先于 `[2, 302]`）。
- **精确求解（不遍历 Hz 范围）**：候选窗口只锚定违规频率——左锚点（以违规频率为窗口最左点）、
  右锚点（以违规频率为窗口最右点，含被扫描边界截断的位置）及其与左邻窗口紧密相邻时的打包位置
  （至多 `max_windows-1` 个 `+width_hz` 移位），候选总数仅 O(n)（n 为违规频率数）。候选排序后以
  双指针定位每个窗口的覆盖频率段与不重叠前驱，再用“至多 k 个互不重叠权值区间”的动态规划精确求解，
  状态按覆盖数、窗口数、起点字典序逐维裁决，整体复杂度 O(n log n + k·n)，与扫描范围的 Hz 宽度无关。
- **响应内容**：每个窗口给出 `window_id`（起点升序从 1 连续编号）、`start_hz`/`end_hz` 与该窗口
  覆盖的违规频率 `covered_violation_freqs_hz`（升序）；顶层另给总体 `covered_violation_freqs_hz`
  （全部窗口覆盖的不同违规频率，升序）与 `uncovered_violation_freqs_hz`（未覆盖的违规频率，升序）。
  **无违规产物时 `window_count=0`、`windows=[]`、两个汇总列表也均为空。**

请求与响应示例（载频 `[100, 300, 400]`、扫描范围 `[1, 10000]`，违规产物为 200 Hz 与 500 Hz；
窗口宽 300 Hz、至多 2 个窗口）：

```bash
curl 'http://localhost:8000/analyses/1/scan-windows?width_hz=300&max_windows=2'
```

```json
{
  "analysis_id": 1,
  "site_id": 1,
  "scan_start_hz": 1,
  "scan_end_hz": 10000,
  "width_hz": 300,
  "max_windows": 2,
  "window_count": 2,
  "windows": [
    {"window_id": 1, "start_hz": 1, "end_hz": 300,
     "covered_violation_freqs_hz": [200]},
    {"window_id": 2, "start_hz": 301, "end_hz": 600,
     "covered_violation_freqs_hz": [500]}
  ],
  "covered_violation_freqs_hz": [200, 500],
  "uncovered_violation_freqs_hz": []
}
```

同一分析把宽度放宽到 301 Hz 时，单个窗口 `[200, 500]` 即可覆盖两个违规频率，并列规则优先返回
窗口数更少的单窗口；宽度 300 Hz 但只允许 1 个窗口时，覆盖数并列取起点字典序最小的 `[1, 300]`：

```bash
curl 'http://localhost:8000/analyses/1/scan-windows?width_hz=301&max_windows=1'
# windows: [{"window_id": 1, "start_hz": 200, "end_hz": 500,
#            "covered_violation_freqs_hz": [200, 500]}]
```

## 新增载频禁用区间（carrier exclusion ranges）

`GET /sites/{site_id}/carrier-exclusion-ranges?candidate_start_hz=a&candidate_end_hz=b`
直接基于站点配置计算，**不执行也不保存分析**。查询参数为**正整数**且
`candidate_start_hz ≤ candidate_end_hz`（缺失、非正、非整数或起点大于终点 → `422`；
站点不存在 → `404`）。

- **禁用判定**：在整数位置 `x` 新增载频，会与每个现有载频 `c` 产生两类新三阶产物
  `2·x − c` 与 `2·c − x`。任一产物落在扫描范围 `[scan_start_hz, scan_end_hz]` 内，
  且到任一**现有**载频或保护频段的距离**严格小于** `guard_interval_hz` 时
  （与分析语义一致），`x` 即不宜新增；**现有载频点本身也视为禁用**。
- **整数 Hz 反解**：违规产物位置是开区间 `(ref − g, ref + g)`（`ref` 为载频或保护频段
  边界，`g` 为保护间隔），整数点端点各内缩 1 Hz；对 `2·x − c` 反解时除以 2
  起点向上取整、终点向下取整，对 `2·c − x` 取镜像区间。`g = 0` 时无违规产物位置，
  仅载频点禁用。
- **最小并集**：全部禁用位置裁剪到候选闭区间后，合并重叠或相邻（整数 Hz 下起点
  相差 ≤ 1）的区间，按起点升序返回 `exclusion_ranges`；无禁用位置时返回空列表。

响应示例（载频 `[1000]`、保护频段 `[480, 520]`、`guard_interval_hz=150`，候选范围 `[1, 10000]`）：

```json
{
  "site_id": 1,
  "candidate_start_hz": 1,
  "candidate_end_hz": 10000,
  "exclusion_ranges": [
    {"start_hz": 666, "end_hz": 834},
    {"start_hz": 851, "end_hz": 1149},
    {"start_hz": 1331, "end_hz": 1669}
  ]
}
```

## 单载频迁移规划（carrier relocation）

`POST /sites/{site_id}/carrier-relocation` 规划把现有载频 `old_hz` 迁移到候选整数
闭区间 `[start_hz, end_hz]` 内的某个频率 `x`，**不修改站点配置，也不保存分析**。
请求体为 JSON：`old_hz`、`start_hz`、`end_hz` 均为正整数 Hz（字段缺失/非法或
`start_hz > end_hz` → `422`；`old_hz` 不是该站点载频 → `422`；站点不存在 → `404`）。

- **可行判定**：把 `old_hz` 替换为 `x` 后，新载频集合按与分析一致的语义判定——
  每个落在扫描范围内的三阶产物 `2·fa − fb`（`fa ≠ fb`）到任一**新**载频
  （含 `x` 本身）或保护频段的距离都不得严格小于 `guard_interval_hz`，
  且 `x` 不得与其余载频重复。
- **线性约束反解（不逐 Hz 枚举）**：全部约束反解为关于 `x` 的禁用整数区间再合并——
  - `x` 与其余载频重复：单点禁用；
  - **未变来源产物** `p = 2a − b`（a、b 均为未替换载频）落在扫描范围内：若 `p`
    到其余载频与保护频段的距离已违规，则任何 `x` 都不可行（直接无解）；否则禁用
    `|p − x| < g` 的位置（**未变来源产物命中新载频**）；
  - 含 `x` 的产物 `2x − c` 与 `2c − x`：仅在产物落在扫描范围内的 `x` 窗口内，
    按"到 `x` 本身 / 到其余载频 / 到保护频段的距离 < g"分别反解线性约束
    （开区间端点内缩 1 Hz；含 `2x` 的约束除以 2 时起点向上取整、终点向下取整）。
- **结果**：禁用区间裁剪到候选范围并合并重叠或相邻区间后，在可行缺口中返回距
  `old_hz` 最近的可行频率 `new_hz`（**等距取较小值**）及其距离 `distance_hz`；
  无可行位置时 `feasible=false`、`new_hz` 与 `distance_hz` 为 `null`（HTTP 200）。

请求与响应示例（载频 `[100, 300, 400]`、保护频段 `[480, 520]`、
`guard_interval_hz=150`，把载频 300 迁到候选区间 `[1, 10000]`）：

```bash
curl -X POST http://localhost:8000/sites/1/carrier-relocation \
  -H 'Content-Type: application/json' \
  -d '{"old_hz": 300, "start_hz": 1, "end_hz": 10000}'
```

```json
{
  "site_id": 1,
  "old_hz": 300,
  "start_hz": 1,
  "end_hz": 10000,
  "feasible": true,
  "new_hz": 550,
  "distance_hz": 250
}
```

无可行位置时（例如候选区间被禁用区间全覆盖，或未替换载频之间的产物本身已违规）：

```bash
curl -X POST http://localhost:8000/sites/1/carrier-relocation \
  -H 'Content-Type: application/json' \
  -d '{"old_hz": 300, "start_hz": 551, "end_hz": 849}'
```

```json
{
  "site_id": 1,
  "old_hz": 300,
  "start_hz": 551,
  "end_hz": 849,
  "feasible": false,
  "new_hz": null,
  "distance_hz": null
}
```

## 载频漂移风险评估（drift risk）

`POST /sites/{site_id}/drift-risk` 评估载频独立漂移下全频域的潜在互调风险，
**不执行也不保存分析**。请求体须为该站点**每个载频**提供唯一且完整的漂移容差
`tolerance_hz`（0～1000000 的整数 Hz；映射缺失、重复、多余或容差非法 → `422`；
站点不存在 → `404`）。

- **共享变量建模**：每个载频 `c` 的实际频率为 `c + δ_c`，`δ_c ∈ [−t_c, +t_c]`，
  不同载频的漂移互相独立，但同一载频的漂移是共享变量。对每个有序来源
  `fa ≠ fb`，产物 `2·(fa+δ_a) − (fb+δ_b)` 的可达区间为
  `[2fa−fb − (2t_a+t_b), 2fa−fb + (2t_a+t_b)]`（每个变量只出现一次，区间精确）。
- **最小可能距离**：枚举全部有序来源（全频域，不按扫描范围过滤），对每个来源
  分别计算到各对象的最小可能距离后取最小——
  - 与**漂移载频** `c` 比较：差值 `(2fa−fb−c) + 2δ_a − δ_b − δ_c` 中同一漂移
    变量合并线性系数（`c==fa` 时 `δ_a` 的系数为 `2−1=1`，`c==fb` 时 `δ_b`
    为 `−2`），可达半径为 `Σ|系数|·t`，最小距离
    `= max(0, |2fa−fb−c| − 半径)`；不把产物区间与载频区间当作独立区间重复放宽。
  - 与**固定保护频段** `[s, e]` 比较：可达区间 `[lo, hi]` 与频段的区间距离
    `max(s−hi, lo−e, 0)`（重叠为 0）。
- **返回内容**：仅最小可能距离严格小于 `guard_interval_hz` 的来源，含来源
  `(fa_hz, fb_hz)`、标称频率、可达区间、最小距离与**全部并列命中对象**
  （载频升序在前，保护频段按 `(起点, 终点)` 升序在后）；结果按
  `(标称频率, fa_hz, fb_hz)` 升序排序，稳定可复现。

请求与响应示例（载频 `[100, 300, 400]`、保护频段 `[480, 520]`、
`guard_interval_hz=150`，容差 `{100: 10, 300: 20, 400: 30}`）：

```bash
curl -X POST http://localhost:8000/sites/1/drift-risk \
  -H 'Content-Type: application/json' \
  -d '{"tolerances": [{"carrier_hz": 100, "tolerance_hz": 10},
                       {"carrier_hz": 300, "tolerance_hz": 20},
                       {"carrier_hz": 400, "tolerance_hz": 30}]}'
```

```json
{
  "site_id": 1,
  "guard_interval_hz": 150,
  "risk_count": 4,
  "risks": [
    {
      "source": {"fa_hz": 300, "fb_hz": 400},
      "nominal_freq_hz": 200,
      "reachable_start_hz": 130,
      "reachable_end_hz": 270,
      "min_distance_hz": 20,
      "hits": [{"type": "carrier", "carrier_hz": 100, "guard_band": null}]
    },
    {
      "source": {"fa_hz": 300, "fb_hz": 100},
      "nominal_freq_hz": 500,
      "reachable_start_hz": 450,
      "reachable_end_hz": 550,
      "min_distance_hz": 0,
      "hits": [{"type": "guard_band", "carrier_hz": null,
                "guard_band": {"start_hz": 480, "end_hz": 520}}]
    },
    {
      "source": {"fa_hz": 400, "fb_hz": 300},
      "nominal_freq_hz": 500,
      "reachable_start_hz": 420,
      "reachable_end_hz": 580,
      "min_distance_hz": 0,
      "hits": [{"type": "guard_band", "carrier_hz": null,
                "guard_band": {"start_hz": 480, "end_hz": 520}}]
    },
    {
      "source": {"fa_hz": 400, "fb_hz": 100},
      "nominal_freq_hz": 700,
      "reachable_start_hz": 630,
      "reachable_end_hz": 770,
      "min_distance_hz": 110,
      "hits": [{"type": "guard_band", "carrier_hz": null,
                "guard_band": {"start_hz": 480, "end_hz": 520}}]
    }
  ]
}
```

## 跨站点冲突检测（cross-site conflicts）

`POST /cross-site-conflicts` 接收 **2～8 个已保存站点 ID** 与非负整数频率容差
`tolerance_hz`（`0` 表示仅同频配对；站点数越界、ID 重复或非正、容差非法 → `422`），
读取每个站点**最近一次**分析结果（不重新扫描；站点不存在 → `404`，存在但尚无分析 → `404`）。

- **配对规则**：仅在**不同站点**的产物之间匹配——两个产物频率差绝对值
  `|fa − fb| ≤ tolerance_hz` 即构成一条跨站点冲突配对；**同一站点**的产物即使频率相近也
  不直接配对。逐对站点在两列按频率升序的产物上以滑动窗口枚举容差窗口内的配对。
- **冲突簇**：把每条产物 `(site_id, freq_hz)` 视为节点、每条跨站配对视为无向边，
  用并查集求连通分量；每个连通分量即一个冲突簇。同站产物只能经**其他站点**的配对
  链式传递归簇（站点 A 的两个近频产物分别与站点 B 的不同产物配对时才会并入同一簇）。
- **每簇字段**：`site_ids`（涉及站点，升序去重）、`products`（簇内全部产物频率及其
  在本站分析中的 `distance_hz`）、`min_distance_hz`（簇内产物最小距离）、
  `pairs`（全部配对明细，含 `frequency_delta_hz`）与 `pair_count`。
- **稳定编号**：簇按簇内最小节点 `(站点 ID, 产物频率)` 升序排序后 `cluster_id` 从 1
  连续编号；配对明细按 `(站点 A, 产物 A, 站点 B, 产物 B)` 升序（A 为较小站点 ID），
  重复请求结果稳定可复现。无跨站近频配对时返回 `200` 且 `clusters` 为空列表 `[]`。

请求与响应示例（三个站点的产物经跨站边链式相连，`tolerance_hz=10`）：

```bash
curl -X POST http://localhost:8000/cross-site-conflicts \
  -H 'Content-Type: application/json' \
  -d '{"site_ids": [1, 2, 3], "tolerance_hz": 10}'
```

```json
{
  "site_ids": [1, 2, 3],
  "tolerance_hz": 10,
  "analyses": [
    {"site_id": 1, "analysis_id": 1},
    {"site_id": 2, "analysis_id": 2},
    {"site_id": 3, "analysis_id": 3}
  ],
  "cluster_count": 1,
  "clusters": [
    {
      "cluster_id": 1,
      "site_ids": [1, 2, 3],
      "products": [
        {"site_id": 1, "freq_hz": 990, "distance_hz": 10},
        {"site_id": 1, "freq_hz": 1020, "distance_hz": 10},
        {"site_id": 2, "freq_hz": 1000, "distance_hz": 10},
        {"site_id": 2, "freq_hz": 1030, "distance_hz": 10},
        {"site_id": 3, "freq_hz": 1010, "distance_hz": 10},
        {"site_id": 3, "freq_hz": 1040, "distance_hz": 10}
      ],
      "min_distance_hz": 10,
      "pair_count": 5,
      "pairs": [
        {"site_a_id": 1, "product_a_freq_hz": 990, "site_b_id": 2,
         "product_b_freq_hz": 1000, "frequency_delta_hz": 10},
        {"site_a_id": 1, "product_a_freq_hz": 1020, "site_b_id": 2,
         "product_b_freq_hz": 1030, "frequency_delta_hz": 10},
        {"site_a_id": 1, "product_a_freq_hz": 1020, "site_b_id": 3,
         "product_b_freq_hz": 1010, "frequency_delta_hz": 10},
        {"site_a_id": 2, "product_a_freq_hz": 1000, "site_b_id": 3,
         "product_b_freq_hz": 1010, "frequency_delta_hz": 10},
        {"site_a_id": 2, "product_a_freq_hz": 1030, "site_b_id": 3,
         "product_b_freq_hz": 1040, "frequency_delta_hz": 10}
      ]
    }
  ]
}
```

## 幂等分析（Idempotency-Key）

`POST /sites/{site_id}/analysis` 支持**可选**请求头 `Idempotency-Key`，用于在重试/并发场景下
保证同一站点的同键请求只产生一份分析结果：

- **首次请求**：携带此前未出现过的 `(site_id, key)` 时，正常执行分析、保存结果并返回 `201`；
  服务在**同一个 SQLite 事务**（`BEGIN IMMEDIATE` 立即取得写锁）内完成登记查重、分析计算、
  分析记录与登记行的写入，分析记录与登记行要么同时可见、要么同时回滚。
- **重复请求**：同一站点再次携带相同键时，**不重新执行分析、不新增分析记录**，直接返回首次
  请求保存的完整结果（`analysis_id`、`created_at`、`products` 完全一致），状态码为 `200`。
- **并发请求**：多个请求并发携带同一键到达时，写锁在事务入口将它们串行化——只有一个请求能
  插入登记行（表上有 `PRIMARY KEY (site_id, key)` 唯一约束），其余请求等待首个事务提交后
  重放首次结果，因此**无论并发多高，同一键至多产生一份分析记录**。
- **按站点隔离**：键的作用域是单个站点，不同站点使用相同键互不影响。
- **失败回滚**：首次请求在计算或写入阶段失败时，整个事务（含登记行）回滚，后续同键请求可
  作为新的首次请求重新执行，不会被卡死在半登记状态。
- **空白键**：请求头为空字符串或仅含空白字符时返回 `422`（不登记、不写分析）。
- **不携带该头**：行为与既有版本完全一致——每次请求都执行并保存一次新分析（`201`），
  现有调用方无需任何改动。

幂等登记只影响 `POST` 是否新增分析；所有现有查询接口（`GET /sites/{site_id}/analysis`、
`GET /sites/{site_id}/analyses`、`GET /analyses/{id}` 及其下游风险分组/停用方案/跨站点检测）
响应结构与语义保持不变，首次分析产生的记录与普通分析记录可同样查询。

```bash
# 首次请求：201，执行并保存
curl -i -X POST http://localhost:8000/sites/1/analysis \
  -H 'Idempotency-Key: 2026-09-22-run-001'

# 重试/重复请求：200，返回同一份结果（相同 analysis_id），不新增记录
curl -i -X POST http://localhost:8000/sites/1/analysis \
  -H 'Idempotency-Key: 2026-09-22-run-001'

# 不带该头：行为不变，仍每次产生新分析（201）
curl -X POST http://localhost:8000/sites/1/analysis
```

建议客户端为每个"逻辑上的一次分析"生成一个稳定的键（如 UUID 或业务流水号），仅在重试
同一逻辑请求时复用；需要再做一次独立分析时使用新键或省略该头。

## 多站点原子批量分析（batch analysis）

`POST /sites/analyses/batch` 接收 **1～10 个不重复的已保存站点 ID**（正整数；
列表为空、超过 10 个、ID 重复或非正 → `422`），**按输入顺序**逐站执行与单站接口
`POST /sites/{site_id}/analysis` 完全相同的分析逻辑并保存结果。

- **任一不存在即整批 404**：事务开始后先确认全部站点存在；任一站点不存在即整批返回
  `404`，**不执行任何分析、不保存任何记录**。
- **单事务整批提交/回滚**：站点存在性检查、各站分析计算与全部分析记录写入都在
  **同一个 SQLite 事务**（`BEGIN IMMEDIATE` 立即取得写锁）内完成；任一计算或写入
  步骤失败则整批回滚，不会出现部分站点已保存分析的中间状态。
- **顺序一致的结果列表**：成功返回 `201`，响应为分析结果数组，第 `i` 项对应请求
  `site_ids[i]`（含完整的 `analysis_id`、`created_at`、`products`）；每个站点新增
  一条分析记录，按输入顺序逐站写入，分析 ID 随写入顺序递增。
- **不支持幂等登记**：批量接口不接收 `Idempotency-Key`，每次调用都为每个站点执行并
  保存一次新分析（分析结果由站点配置确定性决定，重复调用产物内容一致）。
- **单站接口行为不变**：`POST /sites/{site_id}/analysis`（含 `Idempotency-Key`
  语义）与全部既有查询接口保持原行为。

```bash
# 对站点 2、1、3 依次执行分析并在同一事务内保存；响应顺序与输入一致
curl -X POST http://localhost:8000/sites/analyses/batch \
  -H 'Content-Type: application/json' \
  -d '{"site_ids": [2, 1, 3]}'
```

```json
[
  {
    "analysis_id": 1,
    "site_id": 2,
    "created_at": "2026-09-23T08:00:00.000Z",
    "product_count": 6,
    "products": [ /* 站点 2 的分析结果（与 POST /sites/2/analysis 响应同构） */ ]
  },
  {
    "analysis_id": 2,
    "site_id": 1,
    "created_at": "2026-09-23T08:00:00.001Z",
    "product_count": 3,
    "products": [ /* 站点 1 的分析结果 */ ]
  },
  {
    "analysis_id": 3,
    "site_id": 3,
    "created_at": "2026-09-23T08:00:00.002Z",
    "product_count": 1,
    "products": [ /* 站点 3 的分析结果 */ ]
  }
]
```

任一站点不存在时（下例站点 999 不存在）整批拒绝，已存在的站点也不会新增分析：

```bash
curl -i -X POST http://localhost:8000/sites/analyses/batch \
  -H 'Content-Type: application/json' \
  -d '{"site_ids": [2, 999, 1]}'
# HTTP/1.1 404 Not Found
# {"detail":"site(s) not found: 999"}
```

## 分析历史保留清理（retention）

`DELETE /sites/{site_id}/analyses/retention?keep=k` 清理该站点的分析历史：按
**`created_at`、`id` 倒序**（`created_at` 并列时以 id 较大者为更新，与"最近一次分析"
语义一致）保留最近 `keep` 条分析，删除更旧的分析记录**及其在 `idempotency_keys` 中的
幂等登记行**。查询参数 `keep` 为 **1～100 的正整数**（缺失、越界或非整数 → `422`；
站点不存在 → `404`）。

- **单事务整批回滚**：旧分析的幂等登记行与分析记录在**同一个数据库事务**内依次删除，
  任一步失败即整体回滚，不会出现分析已删而幂等键残留（或反之）的中间状态。
- **响应**：
  - `deleted_analysis_ids`：被删除的旧分析记录 ID，按 id **升序**返回；
  - `remaining_count`：清理后该站点剩余的分析记录总数。
- **幂等再清理**：分析总数不超过 `keep` 时删除列表为空、`remaining_count` 为实际剩余
  条数（站点尚无任何分析时为 0），不报错；对同一 `keep` 重复调用结果稳定。
- **清理后再分析**：被删分析对应的幂等键随记录一并删除，之后用**相同键**再次
  `POST /sites/{site_id}/analysis` 会作为新的首次请求执行并保存（`201`）；保留下来的
  分析其幂等键不受影响，同键请求仍返回 `200` 重放。
- **按站点隔离**：清理仅影响路径指定站点的分析，其他站点的分析与幂等键不变。

```bash
# 站点 1 现有 5 条分析：保留最近 3 条，删除最旧 2 条
curl -X DELETE 'http://localhost:8000/sites/1/analyses/retention?keep=3'
```

```json
{
  "site_id": 1,
  "keep": 3,
  "deleted_analysis_ids": [1, 2],
  "remaining_count": 3
}
```

## 站点历史风险（risk history）

`GET /sites/{site_id}/risk-history?limit=N` 汇总该站点**最近 N 条已保存分析**中的产物
违规历史，**只读已保存结果，不触发新分析**。查询参数 `limit` 为 **1～100 的整数**
（缺失、越界或非整数 → `422`；站点不存在 → `404`）。

- **选取窗口**：按 **`created_at`、`id` 倒序**（`created_at` 并列时以 id 较大者为更新，
  与历史保留清理的“最近”规则一致）取最近 `limit` 条分析；分析总数不足 `limit` 时取全部，
  站点尚无分析时按空窗口处理。
- **仅统计曾违规频率**：对在窗口内至少有一次 `violates_guard_interval = true` 的产物频率，
  返回三项统计——
  - `violation_count`：窗口内该频率违规的分析条数；
  - `last_violated_at`：该频率最近一次违规所在分析的 `created_at`；
  - `consecutive_count`：**从最新一条分析起**向旧逐条统计的连续违规条数；任一条分析中
    该频率未违规（产物未出现或标记为非违规）即终止计数，因此最新分析未违规时该值为 `0`，
    计数也不越过选取窗口。
- **排序与空结果**：结果按 `freq_hz` 升序，重复查询稳定可复现；窗口内无任何违规
  （含站点尚无分析记录）时返回 `200` 与空列表 `[]`。

```bash
# 查看站点 1 最近 10 条已保存分析的违规频率历史（不产生新分析）
curl 'http://localhost:8000/sites/1/risk-history?limit=10'
```

```json
[
  {
    "freq_hz": 200,
    "violation_count": 8,
    "last_violated_at": "2026-09-23T08:00:00.000Z",
    "consecutive_count": 3
  },
  {
    "freq_hz": 500,
    "violation_count": 4,
    "last_violated_at": "2026-09-22T08:00:00.000Z",
    "consecutive_count": 0
  }
]
```

上例表示：最近 10 条分析中，200 Hz 产物共违规 8 次（最近一次为最新分析），且最新连续
3 条分析均违规；500 Hz 产物共违规 4 次，但最近一次违规发生在 9 月 22 日——最新一条分析
未违规，连续计数被终止为 `0`。

## 运维备份（maintenance backups）

`POST /maintenance/backups` 供运维在**服务运行期间**为 SQLite 分析库生成可恢复快照，
**无需停服、不影响读写**：

- **在线一致视图**：使用 SQLite [在线备份接口](https://www.sqlite.org/backup.html)
  （`sqlite3.Connection.backup`）复制运行中的库，所得快照事务一致；备份进行时其他请求
  可继续正常读写（复制期间遇到并发写会自动重试）。
- **先临时、再校验、后发布**：备份先写入备份目录下的隐藏临时文件
  （`.intermod-backup-<随机>.tmp`），随后对**备份库本身**执行
  `PRAGMA integrity_check`、`PRAGMA foreign_key_check` 并确认五张业务表齐全；
  全部通过后才用同目录硬链接（`os.link`）原子发布为最终文件名，并 fsync 目录。
  发布前最终文件名在目录中不可见，发布后内容完整可见。
  **任一步失败都只删除本次临时文件，备份目录里不会出现任何可见的半成品，
  也不影响其他请求已发布的备份**。
- **存在即换名、绝不覆盖**：发布用硬链接而非 `os.replace`——目标文件名已存在时
  链接原子失败（`FileExistsError`），服务端随即换一个新的随机文件名重试
  （上限 8 次，实际不会触及）。因此并发或连续两次备份即使恰好取得同名候选，
  也只会各自发布到不同文件，**既有备份的文件名与内容永远保持不变**。
- **响应**：`201 Created`，返回本次**实际发布**的备份文件名 `filename`、
  字节数 `size_bytes` 与 `SHA-256`（`sha256`，64 位十六进制），可据此校验
  取出的文件与服务端文件逐字节一致。文件名形如
  `intermod-YYYYmmddTHHMMSSZ-<8 位 hex>.db`（UTC 时间戳 + 随机后缀，
  每次备份一个新文件，互不覆盖）。

### 令牌配置

接口用请求头 **`X-Backup-Token`** 鉴权，其值必须与环境变量 **`IMD_BACKUP_TOKEN`**
完全一致（服务端使用常量时间比较）：

- **未配置令牌**（环境变量缺失或为空字符串）时，接口**一律拒绝备份**并返回
  `503 Service Unavailable`——无论请求是否带头；
- 令牌缺失或不一致返回 `401 Unauthorized`；
- 备份过程本身失败（复制、完整性校验或发布错误）返回 `500`，且不留半成品。

Docker Compose 会自动加载同目录 `.env` 文件，按以下步骤配置令牌即可，
`docker compose up --build` 单命令启动方式不变：

```bash
# 在 docker-compose.yml 所在目录创建 .env（勿提交到代码库）
echo 'IMD_BACKUP_TOKEN=换成足够长的随机令牌' > .env
docker compose up --build   # 仍然是单命令启动，API 位于 http://localhost:8000
```

也可以直接为单次启动注入令牌：

```bash
IMD_BACKUP_TOKEN='换成足够长的随机令牌' docker compose up --build
```

### 卷内路径

数据库与备份同在命名卷 **`imd-data`**（挂载于容器内 `/data`）：

| 内容 | 容器内路径 |
| --- | --- |
| SQLite 数据库 | `/data/intermod.db`（可用 `IMD_DB_PATH` 覆盖） |
| 备份目录 | `/data/backups`（可用 `IMD_BACKUP_DIR` 覆盖，默认即此） |
| 备份文件 | `/data/backups/intermod-YYYYmmddTHHMMSSZ-<8 位 hex>.db` |

生成备份：

```bash
curl -i -X POST http://localhost:8000/maintenance/backups \
  -H 'X-Backup-Token: 换成足够长的随机令牌'
```

```json
{
  "filename": "intermod-20260924T102915Z-0d2cebc5.db",
  "size_bytes": 45056,
  "sha256": "c79793687b55ec12028c3b6a7fc65945751ee61782ce5610df7826f22084f983"
}
```

未配置令牌（`503`）与令牌错误（`401`）示例：

```bash
# 服务端未设置 IMD_BACKUP_TOKEN
curl -i -X POST http://localhost:8000/maintenance/backups
# HTTP/1.1 503 Service Unavailable
# {"detail":"backup is disabled: IMD_BACKUP_TOKEN is not configured"}

# 令牌缺失或错误
curl -i -X POST http://localhost:8000/maintenance/backups -H 'X-Backup-Token: wrong'
# HTTP/1.1 401 Unauthorized
# {"detail":"missing or invalid X-Backup-Token"}
```

### 取出备份

用 `docker compose cp` 从容器中把指定备份拷到宿主机当前目录，再按响应里的
SHA-256 校验：

```bash
# 查看卷内已有备份
docker compose exec api ls -l /data/backups

# 取出指定备份到当前目录
docker compose cp api:/data/backups/intermod-20260924T102915Z-0d2cebc5.db ./

# 与 POST 响应中的 sha256 / size_bytes 比对
sha256sum intermod-20260924T102915Z-0d2cebc5.db
```

### 从备份恢复

备份是完整、独立的 SQLite 文件（不是 WAL 增量），恢复即用该文件替换数据库路径：

```bash
docker compose stop api
docker compose cp ./intermod-20260924T102915Z-0d2cebc5.db api:/data/intermod.db
docker compose start api
```

恢复后历史站点、分析结果与幂等登记都会回到快照时刻的状态；备份进行期间的在线读写
不影响快照一致性。

## 校验规则（整批拒绝）

- 载频必须为正整数 Hz 且**不得重复**；至少 1 个载频。
- 扫描范围：`scan_end_hz > scan_start_hz`，均为正整数。
- 保护频段：`end_hz > start_hz`，均为正整数。
- 保护间隔：非负整数。
- 任一校验失败 → `422`，**整批拒绝**，不写入任何数据（多表写入在单个事务中完成，失败即回滚）。
- 站点名称全局唯一，重名 → `409`。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/maintenance/backups` | 运维备份：SQLite 在线一致快照，校验完整性后以同目录硬链接原子发布到持久卷 `/data/backups`（目标名已存在即换名重试，绝不覆盖既有备份），返回实际发布的文件名/字节数/SHA-256；需 `X-Backup-Token` 与 `IMD_BACKUP_TOKEN` 一致（未配置 → 503，缺失或不符 → 401） |
| `POST` | `/sites` | 创建站点（频率方案），`201` |
| `GET` | `/sites` | 列出全部已保存方案 |
| `GET` | `/sites/{site_id}` | 查询单个已保存方案 |
| `GET` | `/sites/{site_id}/carrier-exclusion-ranges?candidate_start_hz=a&candidate_end_hz=b` | 候选闭区间内不宜新增载频的最小区间并集（不执行或保存分析） |
| `POST` | `/sites/{site_id}/carrier-relocation` | 单载频迁移规划：求距 old_hz 最近的可行替换频率（不修改站点、不保存分析；old_hz 非本站载频或区间无效 → 422，站点不存在 → 404） |
| `POST` | `/sites/{site_id}/drift-risk` | 载频独立漂移下的全频域潜在互调风险评估（不保存分析；映射缺失/重复/多余或容差非法 → 422，站点不存在 → 404） |
| `POST` | `/sites/{site_id}/analysis` | 执行互调分析并保存结果，`201`；可选 `Idempotency-Key` 请求头：同站点重复/并发同键返回首次结果（`200`）且不新增记录，空白键 `422` |
| `POST` | `/sites/analyses/batch` | 多站点原子批量分析：1～10 个不重复站点 ID 按输入顺序逐站分析并在同一事务内保存，`201` 返回同序结果列表；任一站点不存在 → `404` 且整批不保存，任一步骤失败整批回滚 |
| `GET` | `/sites/{site_id}/analysis` | 查询该方案最近一次分析结果 |
| `GET` | `/sites/{site_id}/analyses` | 列出该方案的全部分析记录 |
| `GET` | `/sites/{site_id}/risk-history?limit=N` | 最近 N 条（1～100）已保存分析的违规频率历史：违规次数、最近违规时间与连续违规次数；只读不触发分析，无违规返回 `[]`，站点不存在 → 404，参数非法 → 422 |
| `DELETE` | `/sites/{site_id}/analyses/retention?keep=k` | 按 created_at、id 倒序保留最近 keep 条分析（1～100），单事务删除旧分析及其幂等键；站点不存在 → 404，参数非法 → 422 |
| `GET` | `/analyses/{analysis_id}` | 按 id 查询已保存的分析结果 |
| `GET` | `/analyses/{analysis_id}/risk-groups` | 按已保存分析结果生成风险分组（违规产物 ↔ 来源载频的二部图连通分量） |
| `GET` | `/analyses/{analysis_id}/mitigation-plan?max_disabled=k` | 按已保存分析结果求最小停用载频集合（精确最小点覆盖，`max_disabled` 取 1～8；上限内无解返回 `feasible=false` 与未消除产物频率） |
| `GET` | `/analyses/{analysis_id}/scan-windows?width_hz=w&max_windows=k` | 基于已保存分析的违规频率规划现场复测窗口（排序+动态规划精确求至多 k 个互不重叠、长度恰为 w Hz 的窗口；`width_hz` 正整数、`max_windows` 取 1～3，宽度超过扫描范围 → 422，分析不存在 → 404，无违规返回空窗口） |
| `POST` | `/cross-site-conflicts` | 跨站点冲突检测：2～8 个站点最近分析的近频产物配对，按连通分量聚合为冲突簇（站点不存在/无分析 → 404，参数非法 → 422） |

### 示例

```bash
# 创建方案
curl -X POST http://localhost:8000/sites \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "site-a",
        "scan_start_hz": 1,
        "scan_end_hz": 10000,
        "guard_interval_hz": 150,
        "carriers_hz": [100, 300, 400],
        "guard_bands": [{"start_hz": 480, "end_hz": 520}]
      }'

# 执行分析（载频 100/300/400 时，2*300-100 与 2*400-300 同为 500 Hz，会聚合为一条）
curl -X POST http://localhost:8000/sites/1/analysis

# 需要重试安全/并发去重时，携带 Idempotency-Key：同键重复请求返回首次结果（200），不新增记录
curl -X POST http://localhost:8000/sites/1/analysis \
  -H 'Idempotency-Key: run-001'
curl -X POST http://localhost:8000/sites/1/analysis \
  -H 'Idempotency-Key: run-001'  # 200，响应与第一次完全一致

# 多站点原子批量分析：对站点 2、1、3 按输入顺序逐站分析，同一事务提交；
# 任一站点不存在返回 404 且整批不保存
curl -X POST http://localhost:8000/sites/analyses/batch \
  -H 'Content-Type: application/json' \
  -d '{"site_ids": [2, 1, 3]}'

# 查询已保存的方案与分析结果
curl http://localhost:8000/sites/1
curl http://localhost:8000/sites/1/analysis

# 清理分析历史：按 created_at、id 倒序保留最近 3 条，其余旧分析及其幂等键在单事务中删除
curl -X DELETE 'http://localhost:8000/sites/1/analyses/retention?keep=3'

# 查询最近 10 条已保存分析的违规频率历史（只读，不触发新分析；无违规时返回 []）
curl 'http://localhost:8000/sites/1/risk-history?limit=10'

# 查询风险分组（违规产物与来源载频的连通分量）
curl http://localhost:8000/analyses/1/risk-groups

# 求最小停用方案（最多停用 2 个载频；仅使用已保存的分析结果）
curl 'http://localhost:8000/analyses/1/mitigation-plan?max_disabled=2'

# 规划现场复测窗口：至多 2 个互不重叠、长度恰 300 Hz 的窗口覆盖尽量多的违规频率
curl 'http://localhost:8000/analyses/1/scan-windows?width_hz=300&max_windows=2'

# 查询候选范围 [1, 10000] 内不宜新增载频的区间（不执行或保存分析）
curl 'http://localhost:8000/sites/1/carrier-exclusion-ranges?candidate_start_hz=1&candidate_end_hz=10000'

# 单载频迁移规划：把载频 300 迁到候选区间 [1, 10000] 内距其最近的可行位置（不修改站点、不保存分析）
curl -X POST http://localhost:8000/sites/1/carrier-relocation \
  -H 'Content-Type: application/json' \
  -d '{"old_hz": 300, "start_hz": 1, "end_hz": 10000}'

# 载频漂移风险评估（每个载频给出容差，不保存分析）
curl -X POST http://localhost:8000/sites/1/drift-risk \
  -H 'Content-Type: application/json' \
  -d '{"tolerances": [{"carrier_hz": 100, "tolerance_hz": 10},
                       {"carrier_hz": 300, "tolerance_hz": 20},
                       {"carrier_hz": 400, "tolerance_hz": 30}]}'

# 跨站点冲突检测（读取各站点最近一次分析，频率容差 10 Hz）
curl -X POST http://localhost:8000/cross-site-conflicts \
  -H 'Content-Type: application/json' \
  -d '{"site_ids": [1, 2, 3], "tolerance_hz": 10}'

# 运维备份：对运行中的库做一致快照（须先配置 IMD_BACKUP_TOKEN）
curl -X POST http://localhost:8000/maintenance/backups \
  -H 'X-Backup-Token: 换成足够长的随机令牌'
```

分析响应节选：

```json
{
  "analysis_id": 1,
  "site_id": 1,
  "product_count": 3,
  "products": [
    {
      "freq_hz": 500,
      "distance_hz": 0,
      "hit": {"type": "guard_band", "carrier_hz": null,
              "guard_band": {"start_hz": 480, "end_hz": 520}},
      "violates_guard_interval": true,
      "sources": [{"fa_hz": 300, "fb_hz": 100}, {"fa_hz": 400, "fb_hz": 300}]
    }
  ]
}
```

风险分组响应示例（上例中违规产物 200 与 500 共享载频 300/400，归为一组）：

```json
[
  {
    "group_id": 1,
    "carriers_hz": [100, 300, 400],
    "product_freqs_hz": [200, 500],
    "source_pair_count": 3,
    "distance_hz": 0
  }
]
```

停用方案响应示例（`max_disabled=2`，停用 `[100, 400]` 即覆盖全部违规来源边）：

```json
{
  "analysis_id": 1,
  "max_disabled": 2,
  "feasible": true,
  "disabled_carriers_hz": [100, 400],
  "eliminated_product_freqs_hz": [200, 500],
  "remaining_product_freqs_hz": []
}
```

上限内无解时（例如三条互不相连的违规来源边但 `max_disabled=2`）：

```json
{
  "analysis_id": 1,
  "max_disabled": 2,
  "feasible": false,
  "disabled_carriers_hz": [1000, 4700],
  "eliminated_product_freqs_hz": [850, 4500],
  "remaining_product_freqs_hz": [12000]
}
```

## 本地开发（不用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# 运行测试（132 个用例，覆盖创建、校验拒绝、聚合、距离、持久化查询、风险分组、最小停用方案、
# 现场复测扫描窗口（覆盖最大化、窗口数与字典序并列、边界截断、参数校验与 404）、
# 新增载频禁用区间、单载频迁移规划、载频漂移风险评估、跨站点冲突检测，幂等键的重复重放、失败回滚与并发去重，
# 多站点原子批量分析的顺序一致、404 整批不保存与失败整批回滚，
# 分析历史保留清理的倒序保留、幂等键联动删除与整批回滚，
# 站点历史风险的窗口选取、违规计数、连续计数终止与参数校验，
# 以及运维备份的令牌校验（未配置 503/错误 401）、快照内容与可恢复性、
# 并发写入一致性、撞名换名重试不覆盖既有备份、并发同名发布的互不覆盖、
# 失败只清理本次临时文件不留半成品与备份库损坏检测）
pytest

# 启动服务，API 位于 http://127.0.0.1:8000
uvicorn app.main:app --reload
```

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `IMD_DB_PATH` | `./intermod.db`（容器内 `/data/intermod.db`） | SQLite 数据库文件路径 |
| `IMD_BACKUP_DIR` | `./backups`（容器内 `/data/backups`） | 备份文件发布目录，与数据库同在 `imd-data` 持久卷内 |
| `IMD_BACKUP_TOKEN` | 未设置（容器内为空 → 备份禁用） | `POST /maintenance/backups` 的 `X-Backup-Token` 校验令牌；缺失或为空时接口返回 503 拒绝备份。建议在 docker-compose 同目录的 `.env` 中设置 |

## 项目结构

```
app/
  main.py       # FastAPI 应用与路由，启动时初始化数据库（含单站分析与多站点原子批量分析、站点历史风险查询、单载频迁移规划、现场复测扫描窗口规划、运维备份接口）
  backup.py     # SQLite 在线备份（在线一致快照 + 临时文件完整性校验 + 硬链接原子发布：撞名换名重试、绝不覆盖既有备份，失败只清理本次临时文件）
  analysis.py   # 三阶互调枚举、同频聚合、最近命中对象计算、风险分组（连通分量）、停用方案（精确最小点覆盖）、现场复测扫描窗口（候选锚定 + 权值区间动态规划）、新增载频禁用区间反解、单载频迁移规划（线性约束反解与禁用区间合并）、载频漂移风险评估（共享变量区间）、跨站点冲突配对与连通分量聚合、站点历史风险汇总
  database.py   # SQLite 建表与读写（单事务批量写入、幂等键原子登记与重放、多站点批量分析单事务提交、历史保留单事务清理、最近分析窗口读取、跨站点最近分析批量查询）
  schemas.py    # Pydantic 请求/响应模型与校验
tests/
  test_api.py   # 端到端测试（TestClient + 临时数据库）
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```

# 产品碳足迹核算与证据 API

基于 **Python + FastAPI + SQLite + Decimal** 的产品碳足迹（PCF）核算服务。
客户端通过 HTTP 登记过程批次（产量、单位、排放因子版本）、物料投入与多产品产出
构成的有向图，并选择 **质量 / 能量 / 经济价值** 分摊法；服务按拓扑顺序计算
每个产品的单位 CO₂e、贡献链（contribution chain），并保存**不可变证据快照**。

## 功能

- **有向过程图**：外部物料 → 过程 → 多产品（联产品）→ 下游过程，Kahn 拓扑排序计算
- **拒绝非法输入**（一次性聚合返回，错误项按路径稳定排序）：
  - `CYCLE_DETECTED` 过程图成环（Tarjan SCC 找出具体环路径）
  - `NEGATIVE_AMOUNT` / `ZERO_AMOUNT` 负量与零量
  - `MISSING_FACTOR` 指定因子版本中缺少因子
  - `FACTOR_UNIT_MISMATCH` / `UNIT_MISMATCH` 因子或投入/产出单位不一致
  - `ALLOCATION_DENOMINATOR_ZERO` 分摊权重之和为零
  - `UNKNOWN_REFERENCE` / `FLOW_EXCEEDS_OUTPUT` / `SELF_REFERENCE` 引用与流量平衡问题
- **Decimal 精确运算**：50 位精度上下文，结果保留 12 位小数（`ROUND_HALF_UP`），
  JSON 中所有数值均为**字符串**传输，杜绝浮点误差
- **贡献链**：每个产品的足迹可追溯到每一条外部物料投入 / 直接排放源，含数量×因子明细、
  传递比例、因子版本与因子值（证据随快照固化）
- **幂等**：以「规范化输入摘要 + 因子版本」的 SHA-256 作为幂等键，重复提交返回同一快照；
  节点重排不产生新快照；`client_request_id` 冲突返回 409
- **并发安全（跨进程/跨连接裁决）**：多个服务进程同时提交时，快照的「输入摘要 / 客户端键」
  裁决在**单条 `BEGIN IMMEDIATE` 事务**内完成，并由 `input_hash` 唯一索引与
  `client_request_id` 部分唯一索引兜底：相同输入并发提交只有一个 `201`，其余竞争者按已提交
  快照重放 `200`；同一客户端键搭配不同输入恰好一个 `201`、其余 `409`，绝不会向客户端泄露
  SQLite 约束异常（`500`）或留下半成品证据。`POST /api/snapshots/{id}/recalculate` 生成新快照
  遵守完全相同的规则
- **因子版本只增不改**：`POST /api/factor-versions` 对同名版本仅在因子 ref 集合、名称、单位
  与 Decimal 数值（`0.10` 与 `0.1` 视为相同）全部一致时幂等返回 200；任一差异返回 409 且
  **不写入/不删除任何行**，并发同名提交由 `BEGIN IMMEDIATE` 事务串行化，新内容必须改用新版本名
- **不可变快照**：因子更新**永不改写历史**；用新因子版本重算会生成**新快照**并通过
  `source_snapshot` 关联，返回逐产品差异（新旧单位足迹、Δ、Δ%）与因子变更清单。
  因子变更清单的新旧值均取自两个快照封存的 `factors_used`，与 `product_diffs` 同源，
  不读取可能已变动的因子目录
- 内置两个种子因子版本：`v2023-01`（基准）、`v2024-01`（因子下降，便于演示重算差异）

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

服务监听 http://localhost:8000 ，SQLite 数据保存在命名卷 `carbon-data`（容器内 `/data`）。

- Swagger 交互文档：http://localhost:8000/docs
- ReDoc：http://localhost:8000/redoc
- 健康检查：http://localhost:8000/health

停止并保留数据：`docker compose stop`；清空数据：`docker compose down -v`。

### 多进程并发核算

快照幂等裁决在 SQLite 层跨连接/跨进程生效，可以直接启动多个指向**同一个数据库文件**的
服务进程做水平扩展（生产中置于同一负载均衡之后即可）。例如在容器/宿主机上再启动一个
监听 8001 端口的实例，与 Compose 实例共享同一数据卷：

```bash
docker compose up --build -d
docker compose exec -d carbon-api \
  uvicorn app.main:app --host 0.0.0.0 --port 8001
# 之后把核算请求任意分发到 http://<host>:8000 与 http://<host>:8001，
# 相同输入并发提交仍只有一个 201（其余 200），同键异输入并发提交为一个 201 + 其余 409。
```

本地不用 Docker 时，等价做法是让两个 uvicorn 进程使用同一个 `CARBON_DB_PATH`：

```bash
CARBON_DB_PATH=data/carbon.db uvicorn app.main:app --port 8011 &
CARBON_DB_PATH=data/carbon.db uvicorn app.main:app --port 8012 &
```

## 本地运行（不用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
CARBON_DB_PATH=data/carbon.db uvicorn app.main:app --reload
```

运行测试：

```bash
pytest -q
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | `/health` | 健康检查 |
| GET  | `/api/factor-versions` | 列出因子版本及全部因子 |
| POST | `/api/factor-versions` | 登记因子版本：新版本 201；内容完全相同的重提交 200；同名版本内容有任何差异 409 |
| POST | `/api/assessments` | 提交核算（首次 201，相同输入幂等重放 200；同 `client_request_id` 异输入 409；跨进程并发同此规则） |
| GET  | `/api/snapshots?limit=&offset=` | 快照列表 |
| GET  | `/api/snapshots/{id}` | 快照完整证据（输入、结果、因子副本） |
| GET  | `/api/snapshots/{id}/products/{product_id}` | 单产品足迹与贡献链 |
| POST | `/api/snapshots/{id}/recalculate` | 用新因子版本生成新快照并返回逐产品差异（201/200/409 裁决规则与提交核算相同） |

> **不可变性**：没有任何修改/删除快照的接口，也没有任何覆盖因子版本的接口。
> 因子版本一经登记即为只增证据：同名重提交只有内容完全一致才幂等返回 `200`，
> 否则返回 `409`（见下方示例 0）；即便如此，快照 `result` 内的因子副本与计算结果也不变。

## 实际请求 / 响应示例

场景：1000 kg 原油经「炼制」过程（天然气 + 电网电力直排）产出汽油 400 / 柴油 500 /
渣油 50 kg（质量分摊）；全部柴油再进入「配送」过程（公路货运）交付。

### 0) 登记因子版本（新版本 201 / 幂等 200 / 冲突 409）

登记一个新版本：

```bash
curl -s -X POST http://localhost:8000/api/factor-versions \
  -H 'Content-Type: application/json' \
  -d '{
    "version": "v2025-01",
    "factors": [
      {"ref": "ef.hydrogen", "name": "绿氢", "unit": "kg",
       "kg_co2e_per_unit": "1.20"}
    ]
  }'
```

新版本返回 `201 Created`：

```json
{"version": "v2025-01", "created": true, "factor_count": 1}
```

用完全相同的内容再提交一次（即使把 `1.20` 写成 `1.200`，Decimal 数值等价）→
`200 OK`，不产生任何写入：

```json
{"version": "v2025-01", "created": false, "factor_count": 1}
```

若同名版本的**任一**因子发生变化——ref 集合（增/删因子）、名称、单位或数值——则
`409 Conflict`，事务整体回滚，目录中既不会出现半个新因子，旧因子也不会被覆盖。
例如把数值改成 `1.05`：

```json
{
  "error": {
    "code": "FACTOR_VERSION_CONFLICT",
    "message": "因子版本 v2025-01 已存在且内容不同；版本一经登记即不可变，请使用新的版本名提交变更",
    "details": {
      "version": "v2025-01",
      "differences": [
        {"factor_ref": "ef.hydrogen", "change": "value_changed",
         "old_kg_co2e_per_unit": "1.200000000000",
         "new_kg_co2e_per_unit": "1.050000000000",
         "delta_kg_co2e_per_unit": "-0.150000000000"}
      ]
    }
  }
}
```

`differences` 逐项列出差异，`change` 取值：`value_changed`（数值变化）、
`unit_changed`（单位变化）、`name_changed`（名称变化）、
`missing_in_request`（新版本删掉了已登记因子）、`added_in_request`（新版本多出因子）。
变化后的因子请改用**新版本名**（如 `v2025-02`）登记。同一请求体内 ref 重复则返回
`422`（`FACTOR_VERSION_VALIDATION_FAILED` / `DUPLICATE_FACTOR`）。并发同名提交由
单条 `BEGIN IMMEDIATE` 事务串行化：恰好一个提交创建版本，其余同内容提交得到 `200`、
异内容提交得到 `409`，不会出现部分写入或行互相混合。

### 1) 提交核算

```bash
curl -s -X POST http://localhost:8000/api/assessments \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON'
{
  "client_request_id": "refinery-batch-2024-001",
  "factor_version": "v2023-01",
  "allocation_method": "mass",
  "external_materials": [
    {"ref": "crude", "name": "原油", "quantity": "1000", "unit": "kg",
     "factor_ref": "ef.crude_oil"}
  ],
  "processes": [
    {
      "process_id": "refining", "name": "原油炼制",
      "batch_quantity": "950", "unit": "kg",
      "direct_emissions": [
        {"source": "natural_gas", "quantity": "1000", "unit": "MJ",
         "factor_ref": "ef.natural_gas"},
        {"source": "electricity", "quantity": "100", "unit": "kWh",
         "factor_ref": "ef.grid_electricity"}
      ],
      "inputs": [
        {"ref": "crude", "quantity": "1000", "unit": "kg"}
      ],
      "outputs": [
        {"product_id": "gasoline", "name": "汽油", "quantity": "400",
         "unit": "kg", "mass_weight": "400", "energy_weight": "450",
         "economic_weight": "600"},
        {"product_id": "diesel", "name": "柴油", "quantity": "500",
         "unit": "kg", "mass_weight": "500", "energy_weight": "400",
         "economic_weight": "300"},
        {"product_id": "residue", "name": "渣油", "quantity": "50",
         "unit": "kg", "mass_weight": "50", "energy_weight": "50",
         "economic_weight": "10"}
      ]
    },
    {
      "process_id": "distribution", "name": "柴油配送",
      "batch_quantity": "500", "unit": "kg",
      "direct_emissions": [
        {"source": "freight", "quantity": "100", "unit": "tkm",
         "factor_ref": "ef.truck_freight"}
      ],
      "inputs": [
        {"ref": "diesel", "quantity": "500", "unit": "kg"}
      ],
      "outputs": [
        {"product_id": "diesel_delivered", "name": "交付柴油",
         "quantity": "500", "unit": "kg", "mass_weight": "500",
         "energy_weight": "400", "economic_weight": "300"}
      ]
    }
  ]
}
JSON
```

返回 `201 Created`（节选；数值均为字符串）：

```json
{
  "snapshot_id": "snap_404159d90ed529d8",
  "input_hash": "404159d90ed529d8fe4023d45cbf737628eb7c026dd8026c47fb7e72107f59a7",
  "client_request_id": "refinery-batch-2024-001",
  "factor_version": "v2023-01",
  "allocation_method": "mass",
  "source_snapshot": null,
  "created_at": "2026-09-29T16:53:28+00:00",
  "idempotent_replay": false,
  "result": {
    "factor_version": "v2023-01",
    "allocation_method": "mass",
    "topological_order": ["refining", "distribution"],
    "processes": [
      {
        "process_id": "refining",
        "direct_kg_co2e": "126.450000000000",
        "incoming_kg_co2e": "80.000000000000",
        "total_kg_co2e": "206.450000000000",
        "outputs": [
          {"product_id": "gasoline", "allocation_fraction": "0.421052631579",
           "allocated_kg_co2e": "86.926315789474",
           "unit_kg_co2e": "0.217315789474"},
          {"product_id": "diesel", "allocation_fraction": "0.526315789474",
           "allocated_kg_co2e": "108.657894736842",
           "unit_kg_co2e": "0.217315789474"},
          {"product_id": "residue", "allocation_fraction": "0.052631578947",
           "allocated_kg_co2e": "10.865789473684",
           "unit_kg_co2e": "0.217315789474"}
        ]
      },
      {
        "process_id": "distribution",
        "direct_kg_co2e": "9.000000000000",
        "incoming_kg_co2e": "108.657894736842",
        "total_kg_co2e": "117.657894736842",
        "outputs": [
          {"product_id": "diesel_delivered", "allocation_fraction": "1.000000000000",
           "allocated_kg_co2e": "117.657894736842",
           "unit_kg_co2e": "0.235315789474"}
        ]
      }
    ],
    "products": [ "/* 每个产品的总/单位足迹与 contributions 贡献链，见下 */" ],
    "factors_used": [
      {"ref": "ef.crude_oil", "unit": "kg", "kg_co2e_per_unit": "0.080000000000",
       "factor_version": "v2023-01"}
    ]
  }
}
```

核对（质量分摊下各联产品单位足迹相同 = 206.45 ÷ 950）：

- 原油 1000 × 0.08 = 80；天然气 1000 MJ × 0.0561 = 56.1；电力 100 kWh × 0.7035 = 70.35
- 炼制合计 **206.45 kgCO₂e**；单位足迹 **0.217315789474 kgCO₂e/kg**
- 配送：柴油份额 108.6579 + 货运 100 tkm × 0.09 = 9 → **117.6579 / 500 = 0.235315789474**

### 2) 幂等重放

用完全相同的请求体再 POST 一次 → `200 OK`，且 `snapshot_id` 不变：

```json
{"snapshot_id": "snap_404159d90ed529d8", "idempotent_replay": true, "...": "..."}
```

即使把 `processes` 数组里两个过程的顺序颠倒，规范化摘要哈希相同，仍命中同一快照；
结果里的 `topological_order` 依旧是 `["refining", "distribution"]`。

#### 并发提交相同核算输入（两个服务进程）

服务支持多进程共享同一个 SQLite 数据库水平扩展（例如
`CARBON_DB_PATH=/data/carbon.db uvicorn app.main:app --port 8000` 起两个实例，
由前端/网关做负载均衡）。下面两个 `curl` **同时**分别打到两个进程，请求体完全相同：

```bash
( curl -s -w '\nHTTP %{http_code}\n' -X POST http://127.0.0.1:8011/api/assessments \
      -H 'Content-Type: application/json' -d @refinery.json &
  curl -s -w '\nHTTP %{http_code}\n' -X POST http://127.0.0.1:8012/api/assessments \
      -H 'Content-Type: application/json' -d @refinery.json &
  wait )
```

裁决在单条 `BEGIN IMMEDIATE` 事务内完成（读之前即取得 SQLite 写锁），结果恰好是：

```text
HTTP 201   # 先提交者创建唯一一份不可变快照
HTTP 200   # 后提交者按已提交记录重放，snapshot_id 与 201 完全相同
```

两个响应的 `snapshot_id` 都是 `snap_404159d90ed529d8`，数据库中该输入摘要只有一行；
旧版本「先查后写」在这个时序下会让后提交者撞上 `UNIQUE(input_hash)` 而收到 `500`，
现在竞争者会重新读取已提交快照并返回 `200`，不泄露任何数据库异常。

#### 同一 client_request_id 搭配不同输入并发提交

相同的 `client_request_id` 是全局幂等键（数据库以部分唯一索引
`idx_snapshots_client_unique` 强制，`NULL` 不受限）。两个进程**同时**用键 `race-key-1`
提交两份不同核算输入（如原油投入量一个是 `1000`、一个是 `900`）时，恰好一个 `201`，
另一个得到 `409 Conflict`：

```json
{
  "error": {
    "code": "CLIENT_REQUEST_ID_CONFLICT",
    "message": "client_request_id=race-key-1 已用于另一核算请求",
    "details": {
      "existing_snapshot_id": "snap_eb7ac0558cca1631",
      "existing_input_hash": "eb7ac0558cca1631da45d587730876d2bfcc5174786e0807151e587c3c252ca7"
    }
  }
}
```

`409` 响应直接给出胜出快照的 ID 与输入摘要，失败方不会写入第二份快照，也不会留下
孤儿行/部分因子行。注意裁决先于新请求体本身的校验：即使新输入另含 422/404 问题，
复用他人 `client_request_id` 仍优先返回 `409`。顺序重复提交语义相同：首次 `201`，
同键同输入此后一律 `200`，同键异输入此后一律 `409`。

### 3) 查询单产品的贡献链（证据）

```bash
curl -s http://localhost:8000/api/snapshots/snap_404159d90ed529d8/products/diesel_delivered
```

```json
{
  "snapshot_id": "snap_404159d90ed529d8",
  "factor_version": "v2023-01",
  "product": {
    "product_id": "diesel_delivered",
    "total_kg_co2e": "117.657894736842",
    "unit_kg_co2e": "0.235315789474",
    "contributions": [
      {"kg_co2e": "42.105263157895", "share_of_product": "0.357861775889",
       "chain": ["external_material", "crude", "refining", "distribution",
                 "diesel_delivered"],
       "kind": "external_material", "source": "crude",
       "factor_ref": "ef.crude_oil", "factor_version": "v2023-01",
       "factor_unit": "kg", "factor_kg_co2e_per_unit": "0.080000000000"},
      {"kg_co2e": "37.026315789474",
       "chain": ["refining", "direct_emission", "electricity", "distribution",
                 "diesel_delivered"],
       "kind": "direct_emission", "source": "electricity",
       "factor_ref": "ef.grid_electricity", "factor_version": "v2023-01"},
      {"kg_co2e": "29.526315789474",
       "chain": ["refining", "direct_emission", "natural_gas", "distribution",
                 "diesel_delivered"],
       "kind": "direct_emission", "source": "natural_gas",
       "factor_ref": "ef.natural_gas", "factor_version": "v2023-01"},
      {"kg_co2e": "9.000000000000",
       "chain": ["distribution", "direct_emission", "freight",
                 "diesel_delivered"],
       "kind": "direct_emission", "source": "freight",
       "factor_ref": "ef.truck_freight", "factor_version": "v2023-01"}
    ]
  }
}
```

### 4) 用新因子版本重算（历史快照不变）

```bash
curl -s -X POST \
  http://localhost:8000/api/snapshots/snap_404159d90ed529d8/recalculate \
  -H 'Content-Type: application/json' \
  -d '{"factor_version": "v2024-01"}'
```

返回 `201 Created`，`new_snapshot.snapshot_id` 是新 ID，`source_snapshot` 指向旧快照：

```json
{
  "source_snapshot_id": "snap_404159d90ed529d8",
  "source_factor_version": "v2023-01",
  "new_snapshot": {
    "snapshot_id": "snap_1a23bd137e949fb0",
    "source_snapshot": "snap_404159d90ed529d8",
    "factor_version": "v2024-01",
    "idempotent_replay": false
  },
  "factor_changes": [
    {"factor_ref": "ef.crude_oil", "status": "changed",
     "old_unit": "kg", "new_unit": "kg",
     "old_kg_co2e_per_unit": "0.080000000000",
     "new_kg_co2e_per_unit": "0.075000000000",
     "delta_kg_co2e_per_unit": "-0.005000000000"},
    {"factor_ref": "ef.grid_electricity", "status": "changed",
     "old_unit": "kWh", "new_unit": "kWh",
     "old_kg_co2e_per_unit": "0.703500000000",
     "new_kg_co2e_per_unit": "0.570300000000",
     "delta_kg_co2e_per_unit": "-0.133200000000"},
    {"factor_ref": "ef.truck_freight", "status": "changed",
     "old_unit": "tkm", "new_unit": "tkm",
     "old_kg_co2e_per_unit": "0.090000000000",
     "new_kg_co2e_per_unit": "0.082000000000",
     "delta_kg_co2e_per_unit": "-0.008000000000"}
  ],
  "product_diffs": [
    {"product_id": "diesel_delivered", "status": "changed",
     "old_factor_version": "v2023-01", "new_factor_version": "v2024-01",
     "old_unit_kg_co2e": "0.235315789474",
     "new_unit_kg_co2e": "0.214431578947",
     "delta_unit_kg_co2e": "-0.020884210527", "delta_percent": "-8.874972"}
  ],
  "changed_product_count": 4
}
```

再查旧快照 `GET /api/snapshots/snap_404159d90ed529d8`，单位足迹仍是
`0.235315789474`，因子副本仍是 `v2023-01` 的数值——历史不会被因子更新改写。
`factor_changes` 的新旧值同样取自**两个快照封存的 `factors_used`**，而非当前因子表：
即使旧版本的目录行在旧版本服务中曾被覆盖，重算时报告的旧值仍是旧快照当初计算所用的值，
与 `product_diffs` 的口径完全一致（本示例中 `ef.natural_gas` 两个版本数值相同，故不出现在
清单中）。重算接口本身也是幂等的：第二次调用返回 `200` 与同一个新快照 ID。

重算的新快照创建与 `POST /api/assessments` 走**同一套跨连接事务裁决**，因此并发场景下
行为完全一致——两个进程同时对 `snap_404159d90ed529d8` 用 `v2024-01` 重算（且携带相同
`client_request_id`）：

```bash
( curl -s -w '\nHTTP %{http_code}\n' -X POST \
      http://127.0.0.1:8011/api/snapshots/snap_404159d90ed529d8/recalculate \
      -H 'Content-Type: application/json' \
      -d '{"factor_version":"v2024-01","client_request_id":"recalc-key-1"}' &
  curl -s -w '\nHTTP %{http_code}\n' -X POST \
      http://127.0.0.1:8012/api/snapshots/snap_404159d90ed529d8/recalculate \
      -H 'Content-Type: application/json' \
      -d '{"factor_version":"v2024-01","client_request_id":"recalc-key-1"}' &
  wait )
# HTTP 201 + HTTP 200，两个响应的 new_snapshot.snapshot_id 相同（snap_1a23bd137e949fb0），
# product_diffs / factor_changes 完全一致（柴油交付 0.235315789474 -> 0.214431578947）。
```

若重算请求携带的 `client_request_id` 已被另一份不同输入摘要的快照占用，同样返回
`409 CLIENT_REQUEST_ID_CONFLICT`，不创建新快照；不带 `client_request_id` 或使用全新键
则正常创建。

### 5) 错误响应示例（环 + 负量 + 零分母一次性返回）

`POST /api/assessments`，过程 A 投入 B 的产品、B 投入 A 的产品，且 B 有负投入量、
质量权重全为 0 → `422 Unprocessable Entity`：

```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "核算请求校验失败",
    "issues": [
      {"path": "processes.graph", "code": "CYCLE_DETECTED",
       "message": "过程投入有向图存在环: A -> B -> A"},
      {"path": "processes[process_id=B].allocation:mass",
       "code": "ALLOCATION_DENOMINATOR_ZERO",
       "message": "过程 B 的mass分摊权重之和为零，无法分摊"},
      {"path": "processes[process_id=B].inputs[0].quantity",
       "code": "NEGATIVE_AMOUNT", "message": "投入数量不能为负"}
    ]
  }
}
```

`issues` 按 `(path, code)` 稳定排序，便于客户端做断言与回归比对。

## 设计要点

- **幂等键**：`sha256(canonical_json(因子版本, 分摊法, 外部物料[], 过程[]))`，
  列表按键排序、Decimal 用规范化文本（`0.10` 与 `0.1` 等价）；显示名与
  `client_request_id` 不参与哈希。
- **分摊与传递**：过程足迹 = 直接排放（数量×因子）+ 外部物料足迹 + Σ上游产品足迹×
  （消耗量/上游批量）；再按所选权重分摊到本过程的全部联产品。贡献链携带每一段的
  缩放与分摊因子，可逐条审计。
- **证据固化**：快照表保存原始请求 JSON、完整结果 JSON（含 `factors_used` 因子值副本）、
  输入哈希、因子版本、来源快照与 UTC 时间戳，只增不改。重算差异的因子新旧值直接读两份
  `factors_used` 副本，不回查因子目录，因此与 `product_diffs` 同源、不受后续目录变化影响。
- **因子版本只增不改**：`factors` 表只有 `INSERT` 路径（无 `UPDATE`/`REPLACE`）。登记在
  单条 `BEGIN IMMEDIATE` 事务内完成「查重 → 比对 → 插入」，配合进程内写锁，保证并发同名
  提交要么完整创建、要么幂等/409，绝不部分写入或互相覆盖。
- **快照并发裁决**：快照身份（`input_hash` 与 `client_request_id`）的「查重 → 裁决 →
  插入」同样在单条 `BEGIN IMMEDIATE` 事务内完成，先取得 SQLite 写锁再读取已提交证据：
  相同摘要重放 `200`，客户端键指向异摘要返回 `409`，否则插入唯一新快照 `201`。库级
  约束为 `UNIQUE(input_hash)` 与 `WHERE client_request_id IS NOT NULL` 部分唯一索引，
  即便极端时序下插入才撞约束，服务层也会回滚并依据**已提交的胜出记录**重放/报冲突，
  不把 `sqlite3.IntegrityError` 透传给 HTTP 调用方。新请求体的因子加载与核算校验只对
  真正的创建者执行（在同一事务内），因此 409 先于 422/404 裁决。旧版非唯一索引在启动时
  自动迁移：同键多快照的旧证据按最早提交行收敛，再建立部分唯一索引。
- **数值口径**：全链路 `Decimal`（进入时从字符串构造，拒绝 NaN/Infinity），
  50 位上下文运算，输出量化到 12 位小数，差异百分比量化到 6 位。

## 目录结构

```
app/
  main.py         FastAPI 路由、异常信封、应用工厂
  models.py       Pydantic 请求模型（Decimal 字符串类型）
  accounting.py   校验聚合 + 拓扑计算 + 分摊 + 贡献链
  graph.py        Kahn 拓扑排序 / Tarjan 环检测
  idempotency.py  规范化输入摘要与 SHA-256
  decimals.py     Decimal 精度、量化、JSON 化
  db.py           SQLite schema 与快照/因子仓储（因子版本只追加；客户端键部分唯一索引 + 旧库迁移）
  seed.py         内置 v2023-01 / v2024-01 因子
tests/            29 个端到端/跨连接并发测试（pytest + TestClient）
docker-compose.yml / Dockerfile
```

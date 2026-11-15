# GTFS 时刻表发布服务

FastAPI + SQLite 实现的 GTFS 版本发布服务：上传 ZIP 包发布新版本，旧版本在新版本发布后仍可按版号查询和下载。

## 快速开始

```bash
docker compose up --build -d
open http://localhost:8000/docs          # Swagger UI
```

样例包位于 `samples/`：

```bash
# 发布第一版
curl -sS -X POST http://localhost:8000/api/v1/versions \
     -F "file=@samples/gtfs_v1.zip" | jq

# 发布第二版（上一版仍然保留）
curl -sS -X POST http://localhost:8000/api/v1/versions \
     -F "file=@samples/gtfs_v2.zip" | jq

# 查询当前版本某班次的完整停站
curl -sS "http://localhost:8000/api/v1/trips/T1/stops" | jq
# 按版号查询（旧版）
curl -sS "http://localhost:8000/api/v1/trips/T1/stops?version=1" | jq

# 跨版本对照线路 R2 的班次与到站变化（首站偏移上限 15 分钟）
curl -sS "http://localhost:8000/api/v1/routes/R2/diff?from=1&to=current&max_offset_minutes=15" | jq

# 下载原 ZIP
curl -sS -OJ "http://localhost:8000/api/v1/versions/1/download"
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/versions` | 上传 ZIP 并发布新版本 |
| `GET` | `/api/v1/versions` | 列出版本（新版本在前），含当前版本号 |
| `GET` | `/api/v1/versions/{version}` | 版本元信息（`version` 可为版号或 `current`） |
| `GET` | `/api/v1/versions/{version}/trips/{trip_id}/stops` | 指定版本某班次的完整停站 |
| `GET` | `/api/v1/trips/{trip_id}/stops?version=…` | 班次停站，`version` 省略时为当前版本 |
| `GET` | `/api/v1/routes/{route_id}/diff?from=…&to=…&max_offset_minutes=…` | 跨版本对照某条线路的班次增减与逐站到站时差 |
| `GET` | `/api/v1/versions/{version}/download` | 下载该版本原始 ZIP |
| `GET` | `/healthz` | 健康检查 |

Swagger：`/docs`，OpenAPI：`/openapi.json`。

## 跨版线路对照

`GET /api/v1/routes/{route_id}/diff` 用于核查两个已发布版本之间某条线路的班次增减与到站变化：

- `from` / `to`：两个版号，均可为 `current`（`to` 默认 `current`）。
- `max_offset_minutes`：非负的最大首站偏移分钟数（默认 0，即首站时刻须完全相同）。

配对规则：班次 ID 在新版中可能已重编，因此只在**完整停站 `stop_id` 序列相同**的班次之间一对一配对，且首站到站时刻之差（绝对值）不得超过偏移上限；先使配对数最多，再使首站时间差总和最小。结果按 `trip_id` 稳定排序，重复调用结果一致。

响应中：

- `matched_pairs[]`：配对成功的班次及其**逐站到站时差（秒）**（`diff_seconds` 为新版减旧版，正为晚点）；
- `removed_trips`：旧版中退出（未能配对）的班次；
- `added_trips`：新版中新增（未能配对）的班次。

超过 24 点的时刻（如 `25:10:00`）一律按服务日秒数比较。两个版号的解析与两版数据读取处于同一 SQLite 读事务，发布交错时不会混入新的 `current`；查询只读取目标线路的班次与停站。线路在两个版本中都没有班次时返回 `404 route_not_found`。

## 发布校验

上传的 ZIP 必须包含 `stops.txt`、`trips.txt`、`stop_times.txt`（UTF-8 CSV）：

- `stops.txt`：`stop_id` 必须唯一，`stop_name` 必填。
- `trips.txt`：`trip_id` 必须唯一。
- `stop_times.txt`：每行必须引用存在的 `stop_id` 与 `trip_id`，`stop_sequence` 为非负整数且同班次内不重复，`arrival_time` 必填；
  按站序排列时到站时刻不得倒退（相等允许）。
- 时刻格式 `HH:MM:SS`，小时可超过 24（例如 `25:10:00` 表示次日 01:10）。
- 每个班次至少要有一个停站。

校验失败返回 `422`，`errors[]` 每项含 `file` 与 `line`（文件内 1 起始的物理行号，含表头行），例如：

```json
{
  "error": "validation_failed",
  "message": "2 validation error(s); no version was published",
  "errors": [
    {"file": "stop_times.txt", "line": 7, "code": "unknown_stop_id",
     "message": "stop_id 'S99' is not defined in stops.txt", "value": "S99"}
  ]
}
```

不合格的包不会产生任何版本，已有版本完全不受影响（`GET /api/v1/versions` 不变）。

## 一致性保证

- 上传字节先写入 `staging/` 唯一临时文件并完成校验，再在 SQLite 写事务（`BEGIN IMMEDIATE`）内原子改名为不可变的 `artifacts/<uuid>.zip`，最后在同一事务里登记版本及所有行并提交。并发上传因此串行化。
- 读者在单个读事务中解析版本号并读取数据，因此每次查询只会看到某一个已提交版本，不会看到半个版本或跨版本混合。跨版对照接口的两个版号与两版数据也在同一个读事务中解析与读取。
- 登记失败会回滚事务并删除刚落地的文件；启动时还会清理残留的临时文件与未被引用的包（保留已提交版本的文件），重启不丢版本。
- 下载前校验文件存在且 SHA-256 与登记的摘要一致，并用同一文件句柄流传已校验的字节；缺失返回 `410`，摘要不符返回 `409`。
- 新版本发布只追加，旧版号仍可查询与下载。发布响应中的 `previous_version` 在同一写事务内读取，并发上传时也准确。

## 数据

- 容器数据目录 `/data`（Docker volume `gtfs-data`）：`gtfs.db`、`artifacts/`、`staging/`。
- 环境变量 `GTFS_DATA_DIR` 可改数据目录，`GTFS_MAX_UPLOAD_BYTES` 可改上传大小上限（默认 256 MiB）。

## 本地运行与测试

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload
pytest
```

## 样例包

| 文件 | 说明 |
| --- | --- |
| `samples/gtfs_v1.zip` | 2 条线路、3 个班次；包含跨午夜 `24:xx` 时刻；站序故意不连续（10/20/30） |
| `samples/gtfs_v2.zip` | 第二版：新增站点、新增班次、调整时刻；同一 `trip_id` 的停站与 v1 有差异，便于验证按版号隔离 |

`gtfs_v2.zip` 中 `T1` 的时刻与站点顺序和 v1 不同，可用来确认 `?version=1` 与 `?version=2` 返回各自版本的数据。

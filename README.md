# 排水流向图：供数、隔离诊断、整批补测与汇水面导出

FastAPI + SQLite 服务。导入一张矩形流向图，回答"哪些格子能顺流到哪个排口"，
把到不了排口的格子隔离并说明原因（缺测 / 出界 / 环路），再按行列成批补测，
只重判改动格和原先流经这些格的上游；每个仍有供数格的排口还会生成一份恰好
覆盖其供数格、保留孔洞的 `MULTIPOLYGON` 汇水面，可按图号下载 CSV 叠到施工地块上。

## 快速开始

```bash
docker compose up --build
# Swagger UI: http://localhost:8000/docs
```

跑一遍演示（导入带环路的示例图 -> 看供数与诊断 -> 补测一格解开环路 -> 再看结果）：

```bash
pip install -r requirements.txt -r requirements-dev.txt
python scripts/demo.py            # 默认 http://localhost:8000
```

本地不装 Docker 时也可以直接起服务：

```bash
DRAINAGE_DB=/tmp/drainage.sqlite3 uvicorn app.main:app --port 8000
pytest                            # 33 个测试
```

SQLite 文件放在 `/data/drainage.sqlite3`（Docker 里由 named volume `drainage-data`
持久化，可用 `DRAINAGE_DB` 覆盖）。

## 数据模型

每格只取一种状态：

| 记号 | 含义 |
| --- | --- |
| `O` | 排口（固定，补测不能新增或改写） |
| `?` | 缺测 |
| `N` `NE` `E` `SE` `S` `SW` `W` `NW` | 八向之一 |

坐标约定：`row` 向下增大、`col` 向右增大，所以 `N` 是 `row-1`、`W` 是 `col-1`。
八个方向都是**指向下游的相邻格**。

## 供数语义

对每个非排口格沿方向一路走，终点只有四种：

1. **到排口** -> 该格供数，记下排口；
2. **走出矩形** -> 隔离，记"出界前的最后一格"；
3. **走到缺测格** -> 隔离，记缺测格和指向它的那一格（`stop`）；缺测格本身也算隔离，但它不流向任何人，没有 `stop`；
4. **走回自己路径上的格** -> 隔离，记整个环路（规范化成从最小格开始，保证同一环路只报一次）。

`GET /grids/{id}/supply` 只返回能到排口的格子，按排口分组；排口本身不参与计数。
`GET /grids/{id}/diagnosis` 返回隔离分组、缺测格清单，以及每个缺测格是否真的被流向。

## 补测与增量重判

`POST /grids/{id}/resurvey` 接收一批 `{row, col, value}`，含义是**按整批补测后的流向重判**：

1. 先整批校验（值合法、在界内、不是排口），任何一条不合法就整批拒绝，一个字节都不写；
2. 只把**真正变化**的格子当作改动格；
3. 用 SQLite 递归 CTE 沿 `cells(grid_id, downstream_row, downstream_col)`
   反向索引向上游走，得到"原先流经这些格"的集合，再加上改动格本身：
   ```
   WITH RECURSIVE up(row,col) AS (
       SELECT ... FROM changed
       UNION
       SELECT c.row, c.col FROM cells c
       JOIN up ON c.downstream_row = up.row AND c.downstream_col = up.col
       WHERE c.grid_id = ?
   )
   ```
   这一步只走上游锥，不扫整张图；
4. 只重判这个集合。集合外的格子用缓存结果直接继承——如果某个下游格的路径经过改动格，
   它本来就在上游锥里，所以缓存不会过期；
5. 方向、供数结果、隔离原因在同一个事务里写入，失败整体回滚。

返回 `supply_transition`（每个排口补测前后的供数格数）、`changes`（每格 before/after）
和 `affected_cells`（这次实际重判了哪些格）。

## 汇水面与 CSV 导出

每个有供数格的排口对应一行汇水面，几何用 OGC WKT 的 `MULTIPOLYGON` 表示，约定：

- 每格是边长 1 的正方形；左上格 `(0,0)` 的左上角为原点 `(0,0)`，`x` 向右、`y` 向上，
  所以格 `(row, col)` 占 `x ∈ [col, col+1]`、`y ∈ [-row-1, -row]`；
- 面**恰好**覆盖该排口的供数格：面积（外环减孔洞）恒等于供数格数；
- 供数格围成的内部空洞保留为内环（顺时针），外环逆时针；
- 只在角点相接的两片区分成两个多边形（4-连通分组，捏合点再拆成简单环），
  不会产生自交的"蝴蝶结"环；
- 排口失去最后一格时，其汇水面行从导出中删除；没有供数格的排口不出现在 CSV 里。

增量规则与补测事务绑定：

1. 一次补测只可能影响"重判格补测前后供数到的排口"，其余排口的汇水面行一字节不动；
2. 候选排口按**供数格集合**（内容，不是格数）比对：集合哈希变了才重新生成 WKT，
   格数相同但成员交换也会重做，集合不变就保留旧面；
3. 集合变空的排口走删除，重新获得供数格时再插回；
4. 流向、供数结果、汇水面在同一个 `BEGIN IMMEDIATE` 事务里写入，生成 WKT 或写库
   失败都会整体回滚——补测后仍能查到原来的流向、供数和汇水面。

`catchments` 表持久化汇水面（WKT、格数、供数集合哈希、更新时间）。升级前已导入的
旧图没有这张表的数据，**不需要重传**：第一次调用导出接口时会在一个写事务里按当前
供数结果惰性回填并落库；回填失败同样回滚，不影响流向与供数查询。

## Swagger 演示：解开环路并下载汇水面

`POST /grids/demo` 导入内置 5×6 示例：外圈是排口，内部有

```
(1,1) S -> (2,1) E -> (2,2) N -> (1,2) W -> (1,1)
```

这个 2×2 环加上汇入它的 (1,3)(2,3)(3,3)，一共 7 格被环路隔离；
(3,2) 向西流入缺测格 (3,1)，再加 1 格隔离。

在 Swagger 里 `POST /grids/1/resurvey`，body：

```json
{"updates": [{"row": 1, "col": 2, "value": "N"}]}
```

把 (1,2) 从 `W` 改成 `N`，环路断开。实测结果：

| | 补测前 | 补测后 |
| --- | --- | --- |
| 供数格 | 3 | 10 |
| 隔离格 | 8 | 1 |
| 环路 | 1 个（4 格） | 0 |
| 重判格数 | — | 7（改动格 + 6 个上游） |
| 排口 (0,2) 供数 | 0 | 7 |
| 排口 (2,5) 供数 | 3 | 3 |

缺测格 (3,1) 补测前后都照样挡住 (3,2)，所以补测后隔离的 1 格是它。

在 Swagger UI（`/docs`）里还可以：

1. 补测前 `GET /grids/1/catchments.csv` 下载 CSV：只有排口 (2,5) 一行，覆盖 3 格；
2. 执行上面的补测后再次下载：变成两行——新排口 (0,2) 的 7 格汇水面出现在
   `regenerated` 里，排口 (2,5) 的面字节不变；
3. 不想下文件时用 `GET /grids/1/catchments` 看同样内容的 JSON。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查（Compose healthcheck 用） |
| GET | `/grids` | 已导入的图 |
| POST | `/grids` | 导入 `{tokens, source_name?}`，201 |
| POST | `/grids/demo` | 导入内置示例图 |
| GET | `/grids/{id}` | 按 token 读回整张图 |
| GET | `/grids/{id}/supply` | 供数结果，按排口分组 |
| GET | `/grids/{id}/diagnosis` | 隔离诊断 |
| GET | `/grids/{id}/catchments` | 汇水面 JSON（Swagger 里方便查看） |
| GET | `/grids/{id}/catchments.csv` | 下载汇水面 CSV，每个有供数格的排口一行，含 WKT |
| POST | `/grids/{id}/resurvey` | 整批补测 + 增量重判 + 汇水面增量重做 |

CSV 表头固定为 `grid_id,outfall_row,outfall_col,cell_count,wkt`，CRLF 行尾、
RFC 4180 引号（WKT 里有逗号，该字段会被双引号包裹），响应带
`Content-Disposition: attachment; filename="grid-{id}-catchments.csv"`。
补测响应额外带 `catchment_transition`：`regenerated` / `unchanged` / `removed`
三个排口坐标列表，说明这次哪些汇水面被重做、保留或删除。

错误码：`404` 图不存在，`409` 试图补测排口，`422` 非法 token / 非矩形 / 越界 /
空批次。

## 代码结构

```
app/grid.py     坐标、方向、token 解析、环路规范化
app/flow.py     单图流向求值（供数 / 三类隔离 / 环路），支持从缓存继承
app/polygon.py  供数格集合 -> 带孔洞的 MULTIPOLYGON WKT（4-连通、捏合拆环）
app/store.py    SQLite schema、事务、上游递归 CTE、汇水面表读写
app/service.py  导入 / 供数 / 诊断 / 整批补测 / 汇水面导出与惰性回填
app/main.py     FastAPI 路由、错误映射、OpenAPI 示例、CSV 下载
tests/          flow、polygon、service、catchment、API 五组测试
scripts/demo.py 端到端演示脚本（含补测前后 CSV 下载与汇水面变化）
```

## 已验证

- `pytest`：65 个测试全过，覆盖八个方向、三类隔离、环路去重、补测前后供数与诊断、
  排口不可改、整批校验原子性、读快照隔离、写后重开仍在、404/409/422，以及汇水面的
  坐标约定、孔洞、角点分离、捏合拆环、集合变化才重做（含等格数换成员）、失去末格删除、
  生成/写库失败整体回滚后旧流向供数汇水面仍可查、旧库首次导出惰性回填、CSV 引号与表头。
- 几何对拍：3000 组随机栅格形状，生成的 WKT 全部通过 shapely `is_valid`，面积等于格数，
  且 `equals(各格方框的并集)`；测试套件里另有 300 组随机形状的 shapely 校验（无 shapely
  时自动跳过，运行时不依赖它）。
- 真实 HTTP：起 uvicorn 跑通 `scripts/demo.py`，实测补测后新增排口 (0,2) 的汇水面被
  生成、原排口 (2,5) 的面保持不变；并手工验证了角点分离成两个多边形、末格丢失后 CSV
  只剩表头、旧图删表后首次导出自动回填且二次读取稳定。
- `EXPLAIN QUERY PLAN` 确认上游递归走的是 `idx_cells_downstream` 索引查找，不是全表扫描。

## 待办

- **Docker 镜像没有在本环境实际构建过**：这里没有 Docker/Compose，只校验了
  `docker-compose.yml` 能被 YAML 解析、`Dockerfile` 结构正确、依赖在 Python 3.12
  Linux 上有可用 wheel。请在装好 Docker 的机器上跑一次 `docker compose up --build` 确认。
- 只展示一张图时没有分页；超大图（数十万格）的导入目前是一条 `executemany`，
  可以改成分批提交。
- 补测历史落在 `resurveys` / `resurvey_changes` 表里，暂未开放查询接口。
- 并发写靠 SQLite `BEGIN IMMEDIATE` 串行化，单实例够用；多实例需要换 PostgreSQL。
- 汇水面 WKT 目前用整数坐标、固定 `MULTIPOLYGON` 文本；若 GIS 侧需要其他坐标系或
  GeoPackage/Shapefile 导出，需要再加一层投影与序列化。

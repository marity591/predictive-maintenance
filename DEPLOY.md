# 部署说明（Docker Compose 一键起全栈）

>  **诚实声明**：本文件是在**本机未安装 Docker** 的环境下编写的 ——
> 因此它经过的是 **YAML 语法校验 + 服务命令本地冒烟测试 + 跨文件引用检查**，
> **没有真的跑过容器**。首次 `docker compose up` 若报错，请把报错发我，我按实际修。
> （这也是工程里应有的态度：没验证过的部分要说清楚，而不是假装跑通了。）

---

## 一、目录与数据流

```
predictive-maintenance/
├── Dockerfile  统一运行镜像（base 零依赖 / modeling 含 numpy+sklearn）
├── docker-compose.yml  6 个服务 + 1 个按需建模服务
├── deploy/mosquitto.conf  Broker 配置（本地演示放开匿名，生产要改）
├── data/  所有持久化数据都在这（容器重建不丢）
│  ├── sensor.db  collector 落库的原始点
│  ├── tsdb.db  时序库（原始 + 1 分钟聚合）
│  └── mosquitto/  Broker 持久化
├── logs/ · docs/ · models/  日志 / 报告与看板 / 模型产物
└── *.py  采集 / 模拟器 / 分析 / 桥接 / 时序库 / 看板 / 建模

数据流：
  simulator ──Modbus TCP:5020──▶ collector ──▶ SQLite(sensor.db)
  ├──▶ bridge ──MQTT:1883──▶ broker
  └──▶ ingest.py ──▶ tsdb.db/ts_raw
  └──▶ rollup ──▶ dashboard ──▶ :8080
```

## 二、服务清单

| 服务 | 镜像 | 作用 | 端口 |
|---|---|---|---|
| `simulator` | 本项目 | Modbus TCP 从站（无硬件时的数据源，带故障注入）| 5020（容器内）|
| `collector` | 本项目 | 轮询从站 → 工程量换算 → SQLite，断线指数退避重连 | — |
| `broker` | `eclipse-mosquitto:2` | MQTT Broker（生产建议换 EMQX）| 1883 |
| `bridge` | 本项目 | SQLite → MQTT 存储转发，失败行先重试；补发前提见 README §11.2 | — |
| `rollup` | 本项目 | 每分钟先运行 `ingest.py` 导入，成功后聚合 1 分钟桶 | — |
| `dashboard` | 本项目 | 定期重生成 HTML 看板 + 静态服务器 | **8080** |
| `modeling` | 本项目(modeling) | 按需跑数据集生成 + 分类 + RUL 回归（不常驻）| — |

## 三、起停命令

```bash
# 起全栈（首次会构建镜像）
docker compose up -d --build

# 看状态与日志
docker compose ps
docker compose logs -f collector
docker compose logs -f bridge

# 打开看板
#  http://localhost:8080/dashboard.html

# 跑建模（按需，不常驻）
docker compose --profile modeling run --rm modeling

# 停止（数据保留在 ./data）
docker compose down

# 连数据一起清掉
docker compose down && rm -rf ./data/*.db ./docs/dashboard*.html
```

## 四、起不来的常见原因（按可能性排序）

| 现象 | 原因 | 处理 |
|---|---|---|
| `collector` 反复重连 | `simulator` 还没起来 | `depends_on` 只保证启动顺序，不保证就绪；本项目采集端有指数退避重连，等十几秒会自己连上  |
| `bridge` 报 `ConnectionRefusedError` | Broker 还没就绪，或 mosquitto 2 默认拒绝匿名 | 确认 `deploy/mosquitto.conf` 已挂载且含 `allow_anonymous true`  |
| 看板 404 | `docs/dashboard.html` 还没生成 | 等一个循环周期（约 30s），或手动 `docker compose exec dashboard python dashboard.py --db data/tsdb.db --hours 24 --out docs/dashboard.html` |
| `rollup` 一直没数据 | collector 只写 `sensor.db`，必须经 `ingest.py` 导入 `tsdb.db` | 查 `docker compose logs rollup` 的导入退出信息，确认源库已有 `sensor_readings`、目标可写及 device 一致；导入失败会跳过本轮聚合，下一分钟重试 |
| 构建时 pip 很慢 | 建模层要装 numpy/sklearn | Dockerfile 里已经指向**清华 PyPI 镜像**  |
| Windows 上挂载目录权限异常 | 容器内用非 root（uid 10001）写宿主目录 | 给 `./data ./logs ./docs ./models` 写权限，或临时改 `user: root` |

## 五、上线到生产时要换什么

| 组件 | 现在（演示/自测）| 生产建议 |
|---|---|---|
| Broker | 容器内 `eclipse-mosquitto`，匿名 | EMQX / Mosquitto 集群 + 账号密码 + TLS |
| 时序库 | `tsdb.py`（SQLite 实现）| **TDengine / InfluxDB**：只替换 `connect/rollup/query_agg`，上层不动 |
| 看板 | 自研单文件 HTML | **Grafana**：接同一个时序库，查询语义一致 |
| 设备侧 | Modbus TCP 模拟器 | 真实 PLC/仪表：Modbus RTU→TCP 网关，或 OPC UA |
| 告警 | 阈值判定 + 看板 | 企业微信机器人 / 邮件 / 短信，带抑制与分级 |

## 六、为什么这样设计（可写进面试）

1. **每个服务只做一件事**：采集、上行、聚合、展示分离 → 任一环节坏掉不影响其它环节，
  也便于单独扩容（例如采集多份、聚合一份）。
2. **数据落盘优先于上行**：先写 SQLite 再异步 MQTT；本地源记录与游标完好、最终恢复连接等条件下补发，不能保证磁盘损坏或下游处理失败时的完整性。
3. **聚合与原始分离**：看板查聚合表、排查查原始表，把查询成本与取值范围解耦。
4. **零依赖的核心脚本**：采集/桥接/时序库/看板都只用标准库 →
  镜像小、构建快、无供应链风险、也不容易因为某个包升级而坏。
5. **诚实的交付说明**：未验证过的部分（没装 Docker）明确标注，而不是"看起来跑通了"。

## 七、显式导入与验证

collector 和 tsdb 使用不同的表结构，不能直接共用数据。`rollup` 服务按顺序执行下面两步，
`&&` 确保导入失败时不聚合；源库尚未创建时导入报错，循环下一轮再试，不会创建伪源库：

```bash
python ingest.py --source data/sensor.db --db data/tsdb.db --device line1
python tsdb.py --db data/tsdb.db rollup --interval 60
```

适配器原样保留 epoch/ts、四项工程量和 quality，`device` 由参数显式指定。`device + epoch` 为
幂等键，同一 epoch 的新源行更新原始点；重复执行不增加行数。数据与 `ingest_progress` 导入进度
同事务提交，中途失败全部回滚，下次可重试。进度按源文件绝对路径和设备隔离，已导入 id 不重放，
从而不会仅因周期重跑而恢复已被 retention 删除的原始点。

源库约定为追加式、id 单调递增，不能原地替换/重编号或修改已导入旧行；迁移源路径须先完成旧库导入，
同一设备的 epoch 必须唯一标识一次采样。`quality` 仅透传，本批次不改变 tsdb 的质量过滤/聚合策略。
S1 的部分 retention 桶、S3 的小数查询上界和 E1 的全 NULL 看板仍有已知问题，见 README §14。

`python -B -m unittest discover -s tests -v` 含本地 TCP 模拟采集一条 → 导入两次 → 聚合的端到端用例。
它验证 Python 命令与数据库链路；本批次没有运行 Docker，也没有真实设备/生产 Broker 验收。

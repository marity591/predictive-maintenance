# 工业设备数据采集与预测性维护

一个从设备侧到看板的完整链路小项目：Modbus 采集 → 本地时序库 → MQTT 上行 → 故障分类与
剩余寿命预测 → 静态看板。全部脚本只用 Python 标准库，建模部分用 scikit-learn。

主要目的是把工业现场的几个环节串起来跑一遍：设备协议、数据落库与降采样、
上行传输的可靠性、以及"模型到底比一条固定阈值强多少"。

## 架构

    Modbus 模拟从站 ──▶ collector（手写 MBAP/PDU）──▶ SQLite: sensor.db
                                                         │
                                          ingest.py（幂等导入，device+epoch）
                                                         ▼
                        bridge（MQTT 3.1.1，存储转发 + 持久化游标）──▶ broker
                                                         │
                                                         ▼
                    tsdb.db: ts_raw ──rollup──▶ ts_agg（降采样 / 保留策略）
                                                         │
                                    ┌────────────────────┴────────────────────┐
                                    ▼                                         ▼
                          train_baseline / train_rul                  dashboard.py（SVG）
                          （分类 / 回归 + 交叉验证）                    + HTML 报告

## 快速开始

    # 一键演示：语法检查 → 启从站 → 采集 → 异常检测 → 生成报告与数据集
    powershell -ExecutionPolicy Bypass -File run_demo.ps1

    # 或按步骤
    python modbus_sim.py                          # 启动模拟从站（5020）
    python collector.py --duration 60             # 采集 60 秒到 data/sensor.db
    python ingest.py --source data/sensor.db --db data/tsdb.db --device line1
    python tsdb.py --db data/tsdb.db rollup --interval 60
    python dashboard.py --db data/tsdb.db --device line1 --hours 24 --out docs/dashboard.html

    # 建模（需要 scikit-learn）
    # 复现下面“效果”一节的数字（默认 3000 条 / 20 段也可跑，数字会不同）
    uv run --python 3.12 --with scikit-learn --with numpy python make_dataset.py --n 4000 --runs 20
    uv run --python 3.12 --with scikit-learn --with numpy python make_dataset.py --n 4000 --runs 20 --noise 2 --fault-gain 1.2 --out data/dataset_hard.csv
    uv run --python 3.12 --with scikit-learn --with numpy python train_baseline.py
    uv run --python 3.12 --with scikit-learn --with numpy python train_rul.py

Docker Compose 起全套（模拟器 / 采集 / Broker / 桥接 / 导入 / 降采样 / 看板）：

    docker compose up --build

## 目录

    collector.py / modbus_sim.py   Modbus TCP 采集与模拟从站（手写 MBAP + PDU）
    mqtt_min.py / mqtt_broker_dev.py  最小 MQTT 3.1.1 实现与开发用 Broker
    mqtt_bridge.py / mqtt_sub.py  存储转发式上行桥接与订阅端
    tsdb.py                       时序库：写入 / 降采样 / 聚合查询 / 保留策略
    ingest.py                     采集库到位时序库的幂等导入
    make_dataset.py               合成退化数据集（可指定运行段数）
    train_baseline.py / train_rul.py  故障分类与 RUL 回归
    dashboard.py                  零依赖静态看板（内联 SVG）
    analyze.py                    单文件特征提取 + 3σ 异常检测 + HTML 报告
    run_demo.ps1 / Dockerfile / docker-compose.yml / deploy/
    tests/                        P0 级别的回归用例（MQTT 游标、导入幂等、文档边界）

## 效果

合成数据 20 条独立运行段（每条 200 点），按运行段分组切分，测试段的运行段在训练时未出现：

    故障分类   阈值基线 F1 0.977 -> 随机森林 0.994（漏报 16 -> 4）【默认集】
               阈值基线 F1 0.943 -> SVM 0.992（漏报 39 -> 5）【困难集：增益 1.2 + 噪声 x2】
    RUL 回归   随机森林 MAE 0.29 h / R^2 0.9994【默认集】
               随机森林 MAE 0.61 h / R^2 0.9976【困难集】

运行 `train_*.py` 会重新生成 `docs/` 下的对比表与报告；
困难集用 `train_baseline.py --data data/dataset_hard.csv` 与 `train_rul.py --data data/dataset_hard.csv` 复现。

## 已知限制

1. 数据集是脚本合成的，RUL 标签由 severity 线性映射得到；真实 RUL 需要失效时间数据，
   且健康样本属于删失数据，正规做法是生存分析。真实轴承数据（如 CWRU）尚未接入。
2. `std_from()` 用平方和相减计算标准差，在"均值很大、方差极小"时会出现灾难性抵消
   （当前 demo 的最大误差量级 1e-10，不影响使用）。
3. `query_agg()` 的端点用浮点参与计算，超出 2^53 的极端整数端点不支持。
4. 删除水位只对升级后新建的库可靠；已存在的库若缺少该记录，需要显式声明历史删除截点。
5. 硬件接入（ESP32）、告警通道、Grafana 接入都还没做。

## 许可

MIT

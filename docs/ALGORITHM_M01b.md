# M01b 质量证据一致性

本轮唯一缺陷：M01 的物理几何和 BOM 已经校验，但直接相信调用方申报的 metrics 与 neighbors。伪造有效长度、密度、邻排 ID 或净距仍可能获得 PASS。

假设与修复：先完成物理实例安全校验，再从已验证的模块与 Shapely footprint 重新计算全部结构质量指标。每项预定义质量指标必须存在并与重新计算结果一致，缺失或篡改均拒绝。JSON 往返会将方向字典的数字键变成字符串，因此使用规范 JSON 比较，避免把合法序列化误判成篡改。

邻排计算从两排真实多边形的主轴投影与横轴中心出发：对每侧选择投影有重合的最近平行排，用 GEOS 实际距离记录净距。不会使用申报的邻排关系或规则值冒充测量值。校验器重新计算邻排表，拒绝未知 ID、缺失关系、伪造净距和错误关系类型。

这轮没有修改排生成策略、模块 DP、排序目标或规则。六个 M01 矩形的 shelves、runs、BOM 和结构指标均与原冻结结果一致；只加强校验证据。M01 原始输出没有覆盖。

复现：

```powershell
.venv\Scripts\python -B -m pytest tests/planning_v2 tests/audit_v2 -q -p no:cacheprovider
.venv\Scripts\python -B -m tests.planning_v2.record_m01b
```

结果：73 passed，7.29秒。包括独立审计新增的伪造质量指标、未知邻排、错误净距攻击，以及全部既有标准矩形与 DP 穷举测试。补充14种结构指标篡改、缺失证据、错误关系及 JSON 往返验证。

对比产物：`outputs/iterations/M01b/comparison.json`。验证结果新增 `quality_metrics_identity=PASS`、`geometric_neighbors_identity=PASS`。`candidate_count`、摘要与摘要定义是生成过程元信息，不是本轮几何质量重算范围。

判断：保留 M01b；不存在排质量或数量回归。仍未进入障碍、真实门店确认、成熟设计质量或最终3D验收。下一步由总负责人复核本轮后批准 M02 障碍切分。

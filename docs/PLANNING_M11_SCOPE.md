# M11：明确人工范围的有限格式容错

2026-09-09。生产入口是 `services/understanding/prepare.py`；`app.parse_cad` 属于已退役技术对照，本轮未改。原 CAD、未知粗框、外墙恢复、实体墙、门候选和精确源线网复核均未扩展。

- 观察：唯一 `HUMAN_PLANNING_SCOPE` / `PLANNING_SCOPE` 即使顶点已表达完整人工范围，也会因为没有 `closed` 标志或重复首点而要求补范围；重复点还可能令显示线网出现零长边。
- 采用：`shared/scope_tolerance.py` 集中设置 1 mm 位移上限；去重复点、局部点/线吸附、GEOS 交点分段与重复段归并、`make_valid` 的 linework 解释。逻辑闭合边只定义规划区域，不成为实体墙。原坐标、原 closed 状态、每项操作、位移、面积变化、退化重复线及派生分段均随来源审计输出，并纳入签名和缓存复核。
- 限制：存在多个大于 1 mm² 的实质区域时直接拒绝；微小碎片仍全量进入修复前后的面积/边界比较，不按面积选最大块。最终只允许一个有效区域；边界 Hausdorff 距离不超过 1 mm，面积差同时不超过原面积 0.1% 和原周长×1 mm。孔洞数量与几何必须原样保留，包括人工环自身编码的孔洞；远端细长毛刺即使面积很小也不能被删。容差不累计扩大，不用于未知框、物理墙、禁放区或入口净宽。
- 验证：理解/范围组合回归 **159/159 PASS**，证据 `outputs/planning_m11/regression/20260909-163416-8909d806/`；补充“点吸附到非相邻源段并分段去重”后，新范围专项 **24/24 PASS**，证据 `outputs/planning_m11/regression/20260909-163630-5ae18818/`。覆盖 LWPOLYLINE/POLYLINE、缺 closed/首点、重复点段、微抖动、连锁吸附拒绝、微孔保护、多区域、粗框、长细毛刺、实际上传原字节保留、签名审计篡改和确认缓存。两次均有 **3389 个受保护文件 SHA/mtime 未变、guard 拒绝 0**。

旧测试契约迁移仅涉及 `tests/understanding_v2/test_prepare.py`、`test_automatic_input.py` 与 `tests/legacy_regression_m07/test_portal_service_review.py`：明确人工环现在可以逻辑闭合；缓存/签名测试改用真实未分类源实体触发必要确认，保留原篡改拒绝断言；门被新墙事实否定后仍撤回门通道，独立人工范围继续保留。发现并修复了格式审计 tuple/list JSON 往返不一致导致缓存拒绝的问题。旧测试与源码副本仍保存在 `outputs/planning_m11/baseline_src/`，未覆盖旧报告。

新通用隔离 runner 在 `outputs/planning_m11/regression_runner.py`：仅写本轮新建的 `regression/` 子目录，校验 M10 冻结输入与 M11 初始保护清单，旧 M02 输出重定向；保护真实 Human 数据，排除活动进程根日志及 `processes.json`。指定 `--path` 时允许其他代理合法编辑当前源码；全量运行默认冻结源码、测试、tools 与依赖清单。浏览器/实时服务测试独立执行，M11 `test_ui.py` 已默认排除。最后将全部 `baseline_src/` 副本也按初始 SHA 纳入保护，专项收集检查 PASS，**4807 个文件未变**，证据 `outputs/planning_m11/regression/20260909-163926-929eeb94/`（此项为 collect-only）。

采用能力与边界核对了 Shapely 官方文档（访问 2026-09-09）：[`make_valid`](https://shapely.readthedocs.io/en/stable/reference/shapely.make_valid.html) 可能返回多区域或低维组成，不能直接当作单一区域；[`node`](https://shapely.readthedocs.io/en/stable/reference/shapely.node.html) 负责交点分段和重复段去除。此处只验证合成几何与既有程序兼容，不代表新真实门店范围已获人工产品验收。

# 微服务2：自动连续排规划

独立启动：.venv/Scripts/python.exe -B -m uvicorn services.planning.app:app --host 127.0.0.1 --port 8102。

POST /plan-v2接收PlanRequest，验证S1签名。AUTO_VALIDATED和必要补充的CONFIRMED均可规划；SYNTHETIC_TEST拒绝生产HTTP。只消费标准数据，不读取CAD、原始业务文件或成熟参考。

runs.py使用Shapely/GEOS完成排带与真实范围/障碍差集，特定一维长度DP组合模板。每排至少两模块，同排紧接，默认排间与排端1200mm。validate_runs独立重建模块、连续排、邻接、指标与清单，不信任声明PASS。

products.py保留来源品类，按配额和蛇形架位分配。真实SKU缺尺寸不精确上架；独立模拟SKU只使用明确尺寸约束。自动验证后输出design_status=VALIDATED，最终hash包括品类结果。无需二维阶段人工审批。

算法不是全局最优或全店疏散求解器；参考对照属于结果后的独立诊断。测试与操作负担见根docs/ACCEPTANCE.md。

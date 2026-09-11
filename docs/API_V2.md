# 标准空间与自主交付契约

当前冻结扩展见AUTONOMOUS_DELIVERY_M05.md；正常HTTP端点见API_CONTRACT.md。M04全面审核及二维阶段停机已由用户新指令覆盖。

PlanningSpace保存boundary及holes、barriers、exclusions、entrances、source_evidence、unresolved和带签名来源状态。AUTO_VALIDATED只由理解服务验证明确源事实，CONFIRMED只表示必要人类补充；SYNTHETIC_TEST只供直接算法测试，生产HTTP拒绝。任何状态都不能跳过几何验证。

PlanRequest只含source、space、business、rules，无ReferenceDesignProfile字段；未知字段拒绝。签名覆盖状态、源/几何、完整业务、规则、证据、确认人、审核字段及说明，变更即失效。确认缓存还绑定解析政策版本并重验源固定约束。

ShelfRun包含方向0/90、中心轴起终点、可用长、深度、完整modules、已用/余长、footprint、neighbors。整排先生成再进行物料长度组合，同排模块首尾接合。LayoutV2.runs/modules与平铺shelves、BOM数量和默认总层数严格一致。正常规划输出design_status=VALIDATED，最终摘要包括商品分配及该状态。

实际ProductPlacementPlan保留来源品类映射；sku_assignments必须为空，真实物理尺寸未知。SyntheticProduct/ShelfLevel只用于另行模拟明确尺寸、Facing和净高约束，不混入真实商品分配。

ReferenceDesignProfile只作为输出后的独立对照数据；不能由规划服务读取或作为参数。参考实际面积未知，跨空间总量/密度不作为自动通过依据；独立诊断不为门店老板增加阶段审批。

默认基础规则：排间1200mm、边界/墙退让100mm、排端1200mm、每排至少两模块。这是项目设计参数，不是法规认证。物料高度/零件缺失，三维展示假设独立注明。

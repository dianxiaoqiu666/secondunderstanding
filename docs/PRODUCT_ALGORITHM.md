# M03：品类分区与独立模拟 SKU 摆放

`services/planning/products.py` 提供三个纯函数：

```python
assign_categories(business: Business, runs: list[ShelfRun]) -> ProductPlacementPlan
allocate_synthetic(products: list[SyntheticProduct], levels: list[ShelfLevel]) -> dict
validate_synthetic(products: list[SyntheticProduct], levels: list[ShelfLevel], result: dict) -> dict
```

函数无文件、数据库或网络 I/O，不读 CAD、不读参考图，不解析商品名称/规格中的尺寸。共享接口由根维护，本模块未修改契约或规划生成器。

## 真实商品只做品类标签分配

来源是 `business.products[].category`。以 `>` 分隔后的第一级作为规划类别，原始完整类别路径及其记录数全部保留在 `source_paths`，不根据名称猜品类。

初始配额使用商品**记录数**，不是销量、库存、利润或需求预测：

1. 按记录数降序、类别名排序；架位足够时每个有效顶级类别至少一架。
2. 剩余架位按记录数比例分配，整数部分向下取整，剩余名额按最大余数分配，类别名作确定性平局顺序。
3. 类别多于架位时，记录数较多的类别先获得一架，其余记录 `INSUFFICIENT_SHELF_SLOTS`。
4. 缺少、空白、非法 category 或 `>` 前无顶级类别时，记录 `MISSING_OR_INVALID_SOURCE_CATEGORY`，不从商品名称补造。

货架按方向分组、跨排坐标排序，奇偶排交替遍历模块方向，形成蛇形序列。每个类别获得序列的连续片段，尽量减少跨排远跳；每个货架只分给一个类别。任意障碍形状下不宣称全局距离最优。

每条类别分配明确表达 `category → zone_id → run_assignments → shelf_ids`。`zone_geometry_mm` 是所分货架真实 footprint 的 **Shapely 精确并集**，可能为 MultiPolygon；不使用包围矩形或 convex hull，因此不会把中间过道、柱或孔洞包成品类区。

此结果是类别标签与初始货架配额，不代表真实 SKU 已放下、容量可行、温控设备匹配或商品陈列合格。真实 `sku_assignments` 始终为空，状态始终为 `UNAVAILABLE-DUE-TO-MISSING-PHYSICAL-DATA`。真实商品逐记录列入 `unassigned_products`，保留来源行号、条码和完整分类路径；不得根据获批货架默认层数猜商品可用层净高。

## 独立模拟 SKU 算法

只接受显式 `SyntheticProduct` 和 `ShelfLevel`，输出 `SYNTHETIC_ONLY_NOT_REAL_SKU`，不混入真实 ProductPlacementPlan。

- 顺序：`demand_weight` 降序，`product_id` 升序。
- 每个 SKU 的 `minimum_facings` 一次放在一个明确的 `(shelf_id, level_index)` 上，SKU 不拆到多个层位、不重复分配。
- 只铺一排、从左至右占用层板宽度；候选层位必须满足类别、深度、净高和 `unit_width × facings` 宽度。
- 在可行层位/方向中选择剩余宽度最小的 best-fit 候选，以层 ID、层索引和未旋转优先作稳定平局顺序。
- 仅 `ALLOW_XY_SWAP` 可以交换商品宽深；不会交换高度或旋转到侧躺。
- 净高严格取 `ShelfLevel.clear_height_mm`，从不由真实货架默认层数推断。
- 只保证最低 facings 与明确约束，不宣称通用装箱最优、最大销售额、最大容量或最佳陈列。

输出分配行包含 `product_id/category/shelf_id/level_index/facings/rotated_xy/unit_width_mm/unit_depth_mm/height_mm/x_start_mm/x_end_mm/y_start_mm/synthetic`。每个未分配商品恰有明确原因：`NO_LEVELS`、`CATEGORY_MISMATCH`、`TOO_HIGH`、`TOO_DEEP`、`TOO_WIDE_FOR_MINIMUM_FACINGS`、`CAPACITY_EXHAUSTED`。

重复商品 ID、重复层位标识、无效尺寸、非法 facings 等输入直接拒绝。独立 validator 不调用分配器，重查来源完整性、唯一分配、层宽/深/高、XY 权限、最低 facings、同层宽区间重叠、未分配理由，以及由真实区间重算的层板已用/剩余宽度。输出自带 `validation` 也不能免除外部复核。

## 本轮真实数据与模拟验证

由测试夹具通过已获批的 `load_business()` 读取数据，生产商品函数不读取文件。本轮结果见 `outputs/iterations/M03/metrics.json`：

| 指标 | 实跑结果 |
| --- | --- |
| 真实商品记录 | 7839 |
| 完整类别路径 | 174 |
| 顶级类别 | 37 |
| 合成 large 空间架位 | 208 |
| 分配顶级类别 | 37 / 37 |
| 真实 SKU 精确摆放 | 0；7839 条明确缺尺寸 |
| 独立模拟例 | 5 SKU、2 层位；2 分配、3 有原因未分配 |
| 模拟约束 validator | PASS、0 违规 |
| 相同输入/逆序输入复算 | 确定性一致 |

这里的 208 架位是标准合成测试空间，**不是正式 CAD 的人类确认或实际门店验收**。原始商品/物料 hash 由 `load_business()` 先核验，并落入 M03 指标记录。

14 项自身测试覆盖真分类路径、缺类/类别过多、蛇形/唯一架位、精确区域、纯函数、最低 facings、需求顺序、旋转、净高、重复标识、非法数值、独立篡改检查和确定性。另有独立审计发现层板 `level_usage` 派生字段可被篡改；本轮已修正为按真实分配区间重算校验，保留该失败与修复的原因，不以已有 PASS 字典代替验证。

修复后复跑结果：自身 **14/14 PASS**，独立审计 `tests/audit_v2/test_product_audit.py` **24/24 PASS**。独立审计由另一智能体编写，并检查了真实 7839 记录、完整 source category、精确 zone union 和无生产参考依赖。最终根级集成与浏览器验收仍由根执行。

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONDONTWRITEBYTECODE='1'
$env:TEMP="$PWD/.tmp"
$env:TMP="$PWD/.tmp"
.venv/Scripts/python.exe -m unittest discover -s tests/products_v2 -v
.venv/Scripts/python.exe -m tests.products_v2.record_m03
```

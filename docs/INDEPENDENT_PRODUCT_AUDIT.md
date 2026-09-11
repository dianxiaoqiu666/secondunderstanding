# D 阶段独立商品算法审计

范围：仅审计 `assign_categories`、`allocate_synthetic`、`validate_synthetic`，审计角色只修改本报告及 `tests/audit_v2/test_product_audit.py`。先与实现者核对输出字段，再建立独立物理谓词；不修改生产代码。

## 首轮结果

独立命令：

```powershell
.\.venv\Scripts\python.exe -B -m pytest tests/audit_v2/test_product_audit.py -q -o addopts='-p no:cacheprovider --import-mode=importlib'
```

首轮 **23 passed，1 failed**。发现一个派生证据缺口：实际模拟分配占用层宽 200 mm，把 `level_usage.used_width_mm` 改为 0，`remaining_width_mm` 改为 900 后，`validate_synthetic()` 仍返回 PASS。物理分配合法不意味着自报容量统计真实；保留 `test_synthetic_validator_rejects_corrupt_deliverable[fake_usage]`。

修复后独立重跑：**24 passed in 1.55s**。验证器现在从实际分配末端重算使用／剩余宽度，核对层标识唯一且完整；原始伪造用例被拒绝，缺口 **CLOSED**。其他原有通过项没有回归。

## 独立覆盖及已通过项

- 按原始 SyntheticProduct 和 ShelfLevel 独立重算旋转后的宽／深、完整 minimum_facings 占宽、净高、层宽及非重叠，不以实现者的 validation=PASS 代替检查。
- 覆盖允许／禁止旋转、非整数尺寸、层级品类限制、多层多架以及输入顺序反转确定性。
- 独立构造 NO_LEVELS、CATEGORY_MISMATCH、TOO_HIGH、TOO_DEEP、TOO_WIDE_FOR_MINIMUM_FACINGS、CAPACITY_EXHAUSTED 单因素案例，核对每个未分配原因；全部输入商品必须恰好出现在分配或未分配中一次。
- 恶意重叠、重复、丢失商品、虚假未分配原因和篡改商品高度都被独立验证器拒绝。重复商品 ID／层标识、原地改坏的负尺寸、空商品 ID 明确拒绝。
- 品类区域以分配实例的真实 footprint 做独立 GEOS union，并逐字段核对 shelf_ids 与 run_assignments；区域保持跨过道的 MultiPolygon，不用全店 AABB、Hull 或成熟设计坐标代替。
- 品类字段缺失、空值及非字符串均不从商品名称、规格或附加尺寸字段补齐类别／物理摆放。所有真实 `sku_assignments` 为空，每条实际商品都有缺少物理尺寸原因及源记录索引。
- 经 `services.understanding.app.load_business()` 只读加载获准正式商品目录，实际核对 **7839 条**；品类计数独立从 `category` 字段重算，已分配／未分配计数完整。
- 对生产 `products.py` 做 AST 直接 I/O／参考依赖检查；测试读取正式目录不代表生产规划模块自行读取文件。

## 有限结论

品类分配是按源记录数量给架数配额并沿蛇形遍历分配标签；它不是销量、库存、需求、容量或精确陈列优化。蛇形列表中的相邻项不保证障碍切分后在物理空间上全部相邻，类别 zone 也不保证单个连通 Polygon；本报告只确认区域严格对应被分配的实际货架并完整保留源品类路径。

模拟 SKU 的显式层间净高仅来自 ShelfLevel 测试输入；不能依据默认层数推断真实净高，也不能把模拟结果标为真实商品陈列。生产确认、正式店域、成熟布局质量及最终浏览器交付不由本审计宣告通过。

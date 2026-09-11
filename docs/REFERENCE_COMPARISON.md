# 独立离线参考对照库

接口：`compare_layout(layout: LayoutV2, base_profile: dict, supplement: dict) -> dict`，位于 `tools/reference_profile/comparison.py`。函数没有文件或网络 I/O，不修改输入，不生成货架；调用层先核验冻结文件 SHA256，再传入已解析字典。生产规划服务不得导入此工具。

## 测量依据

自动方案从 `shelves[].footprint_mm` 重算模块真实长深、方向、面积和连续排，不信任 `layout.metrics`、`layout.validation`、声明的排数或 `design_status=ACCEPTED`。声明排成员仍会核对平铺模块同源、端接间隙和横向偏移，防止把分散模块只改成相同排 ID。

参考从两个冻结子集的 ACTUAL footprint 重算：当前为 120 个模块、严格 1 mm 分组 44 排，并列保存 LINE 6 mm 近共线诊断后的 36 排。6 mm 仅用于原参考的约 5.2 mm 横向错位，绝不应用到自动方案。两子集不是完整门店库存，完整库存数量仍 UNKNOWN。

结果提供模块/排数量、均模块数、孤立模块和孤立排比例、多模块连续总长、尺寸分布、物料 ID 混合及混合排数、方向、相邻平行净距分布、背靠背接触对、短碎排、面积与密度。排尾剩余长度保留为声明值，明确未独立证明可用排长，不能用该声明强行证明最优组合。

## 可以自动判定的范围

`automatic_structure_status` 仅是该比较器实际检查的结构与几何项目：

- 自动端接与横向共线容差 0.001 mm，只吸收数值误差；短排以结果中的 `min_modules_per_run` 规则为准。
- 几何越界/孔洞、边界退让、禁放/入口侵入、墙侵入或退让、模块面积重叠、不同重建排的距离不足，以及声明几何/成员不一致，会令 `automatic_structure_status=FAIL` 和 `gate_c_status=FAIL`。
- 模块重叠和域外面积容差为 0.001 mm²；墙和线性距离数值容差为 0.001 mm。
- 独立几何检查不会重新认证 PlanningSpace 的人类签名；未重做完整排端过道检查，返回明确 `NOT_CHECKED_USE_FULL_PRODUCTION_VALIDATOR`。完整生产 validator 仍必须通过。

单模板本身不是错误：可用排长可能恰好被一种规格整除。比较器提示需要解释有效长度和其他组合，但不靠“必须混用”伪造质量改善。均模块数低于参考时同样返回审查提示，必须结合空间形状、障碍和规则解释。

## 不允许自动判 PASS 的范围

`gate_c_status` 在此库中仅有 `FAIL` 或 `REQUIRES_REVIEW`，没有 PASS 路径；`product_acceptance_status` 始终 `NOT_ASSESSED`。SYNTHETIC_TEST 永远不能满足成熟设计 Gate C 或真实 CAD Gate E。即便 `confirmation.state=CONFIRMED`，仍需要调用层认证实际空间、入口、功能禁放区，并进行 2D 对照和人类产品质量验收；任何 ACCEPTED 字段、备注或 validation 字典都不能代替外部证据。

参考真实店面积 UNKNOWN，生产输入即便自称 CONFIRMED 也不能建立“同一真实门店已确认”的可比前提。因此：

- 禁用跨空间模块数和有效总长比例的硬通过门。
- 禁用真实门店占地密度通过门；自动侧可报告所提供确认几何的面积，但其认证仍由外层负责。
- 货架外接矩形密度只作为同定义的描述性代理。它包括跨功能区空白，忽略外接矩形外过道，不能冒充真实店面积或据此证明布局不稀疏。
- 原 INSERT 子集实际净距 550–690 mm，当前生产一般采用 1200 mm；两者必须并列说明。不能降低生产过道以追赶成熟参考的模块数或密度。
- 原参考文字到排的距离不等于确认功能区；功能关系及入口通行需要独立 2D 人类审查。

## 测试

`tests/reference_profile/test_comparison.py` 覆盖实际几何重算、120/44/36 基准、SYNTHETIC 禁止通过、伪造 ACCEPTED 无效、分散模块不被排元数据掩盖、重叠不能被 validation 掩盖、函数无 I/O/不修改输入/确定性，以及单模板的条件性解释。运行：

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONDONTWRITEBYTECODE='1'
$env:TEMP="$PWD/.tmp"
$env:TMP="$PWD/.tmp"
.venv/Scripts/python.exe -m unittest discover -s tests/reference_profile -p test_comparison.py -v
```

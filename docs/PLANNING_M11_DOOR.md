# M11 门口与路线证据

本模块已实现真正店外起点到取货面的路线。测试资料仍为合成场景；真实门店效率和人工产品验收未由这些检查证明。旧 M10 的店内入口结果保留为技术对照。

## 明确输入与采用方式

旧路径从门边进入店内，不能证明店外入口可达。M11 在原输入上增加 `physical_perimeter_walls` 和 `door_connection`，保持旧 schema 可读。`entrance_opening_mm` 继续唯一表示开口；外围实体墙使用原 `Wall` 模型，例如 `{"id":"left-low","start_mm":[0,0],"end_mm":[0,4800]}`。

下面是左墙门的连接片段，开口为 `[0,4800]` 至 `[0,7200]`；其余外围实体墙也应明确输入：

```json
{
  "door_connection": {
    "id": "test-door",
    "outside_point_mm": [-600, 6000],
    "inside_point_mm": [600, 6000],
    "connection_region": {
      "boundary_mm": [[-600,4800],[600,4800],[600,7200],[-600,7200],[-600,4800]],
      "holes_mm": []
    },
    "provenance": "SYNTHETIC_TEST"
  }
}
```

`workpoints.entrance` 必须等于店外点。收货、拣货起点和打包点保持各自明确的店内坐标。通道规划只以显式店内连接点加入店内骨架，实际入口路径保留“店外点—门中点—店内点”。外部段限于输入连接区域和开口法线；不建立店外绕墙图，也不将外部点吸附进店内。

闭合 scope 是逻辑规划范围。路线只在明确开口处越过该范围；所有实体墙原样并入 `operations_request` 的 barriers。输入开口不会自动切掉实体墙。因此同一开口增加封门实体后，输入仍有效，店外可达性必须失败。门配置错误用 `SCENARIO_CONFIGURATION_INVALID`；不能误报成等待 CAD 的 `NOT_READY`。

## 通道与数值口径

保留原环路和入口联络带，另提供两个可单独比较的 M11 机制：`COMPACT_PERIMETER_LOOP` 仅去掉排端通道多加的货架退距；`WALL_FRONT_LOOP` 在两个方向都按墙架前沿预留，供混合方向沿墙排使用。二者均有可选入口联络带，均不缩小既定通道净宽。M11 墙架前沿采用 `max(boundary_clearance_mm, wall_clearance_mm) + 单侧深度 + 半通道宽`。布局分支和组合校验另由布局模块共同验证，孤立通道不构成合格方案。

内部路线仍用 Shapely 逐段核验净距，NetworkX 边权为实际毫米长度；恰好 1200 mm 通道保留中心线，容差 `1e-6` mm。背靠背组合整体是不可穿实体，两侧逐模块取货面各有站位、入口/拣货/收货路径。固定商品位置和确定性多件任务策略未变。

`minimum_route_clear_width_mm` 是所有实际路线段到实体墙、障碍和货架最小距离的两倍，并计入门连接区域的限制；闭合 scope 的开口线不会把该值错误置零。门口另给 `entrance_connection.actual_clear_width_mm`。这是选定中心路线的净宽证据，不等于整个店铺所有位置的横断通道宽度，也不是通行人流量。校验器重算这些值，不能仅改显示值获得通过。

`full_operations_proven` 位于评价顶层，只有全部固定任务、全部取货面和显式店外入口均通过才为 true。旧无门输入可保留技术 `PASS`，但 `outside_entry_proof=MISSING_LEGACY`、完整作业证明为 false；旧输入摘要排除两个未使用的新增默认字段，保持冻结 M10 摘要不变。

## 已核验与边界

- `tests/planning_m11/test_door_routes.py`：11 项通过，覆盖真店外起点、双面两侧、同门封闭、其他边界缺口不能绕门、位置不能吸附、连接区不足净宽、实际宽度篡改、JSON 复验、旧摘要和三类通道几何。
- `tests/planning_m10/test_routes.py`：13 项通过；原规则、固定任务、孔洞、背靠背禁穿和篡改拒绝继续通过。
- 只读复核：实体墙已进入几何障碍。边界与墙退距同为 100 mm 时安全区域没有叠成 200 mm；墙退距 300 mm、边界 100 mm 时执行 300 mm。
- 冻结 M10 对照适配器只在旧通道生成副本中换成明确店内点；布局与路线测量仍绑定同一完整新输入。`integration-short-1/op-short.json` 的冻结 M10 布局重算后，五个入口/拣货/补货距离指标与保存值一致，入口路径均从真正店外点开始。未读、未评估新的留出场景。
- `outputs/planning_m11/check_closed_doors.py` 只读取主场景清单与指定目录的同名结果，新增一个同开口实体墙，保留货架、任务和工作点，输出新证据文件且拒绝覆盖。独立合成夹具冒烟检查通过：16 个面原先均有店外路径，封门后店外可达为 0、店内拣货仍可达 16，3 张拣货/2 组补货和商品位置均保留。证据在 `outputs/planning_m11/door-checker-smoke-v1/smoke-report.json`。最终主矩阵另行复验如下。

## 最终实际服务响应的封门复验

对 `outputs/planning_m11/final-main/{id}.json` 中全部 16 份最终响应分两批执行冻结 checker；每份均与 `live-main-final/{id}-response.json` 字节一致。第一批 5 项通过后不重跑，第二批 11 项通过，两批原件保留为 `closed-door-final-part1.json` 和 `closed-door-final-part2.json`。合并结果 `outputs/planning_m11/closed-door-final.json` 恰好覆盖冻结主清单 16 项，无重复、无缺项；两批的 checker、路由、契约、主清单 SHA256 均相同且等于当前文件。

| 主场景 | 开放时取货面数 | 封门后店外可达数 | 封门后店内拣货可达数 |
| --- | ---: | ---: | ---: |
| medium | 83 | 0 | 83 |
| square | 57 | 0 | 57 |
| long | 62 | 0 | 62 |
| column | 77 | 0 | 77 |
| edge_block | 82 | 0 | 82 |
| entrance | 78 | 0 | 78 |
| concave | 63 | 0 | 63 |
| op-short | 83 | 0 | 83 |
| op-long | 88 | 0 | 88 |
| op-corner | 83 | 0 | 83 |
| op-shared | 95 | 0 | 95 |
| op-split | 96 | 0 | 96 |
| op-square | 57 | 0 | 57 |
| op-column | 74 | 0 | 74 |
| op-holdout | 62 | 0 | 62 |
| op-independent-holdout | 108 | 0 | 108 |

共核验 1248 个跨场景取货面；16 项开放时完整作业证据均通过，实际路线最小净宽均为 1200 mm，门连接最小净宽为 2300 mm。封闭同一门口后，输入仍合法，所有店外路径失败、门净宽为 0；店内取货仍全部可达，全部固定拣货/补货任务与商品位置保留，完整方案的路线汇总指标置为 `None`。这是对同一实体门的反事实复验，未移动货架、工作点或任务。

合并证据 SHA256：`15465ee382c2740c7498e7b7179b482df3d90e705e3e557d21c9c45e700419f3`。这里名称含 holdout 的两项是已提升为主矩阵的旧反例；本模块未读、未评估本轮新留出场景。这 16 项仍为固定合成业务任务，人工产品验收及真实门店作业效果仍待复验。

不适用范围：任意店外绕行、工业仓储、交通仿真、真实销售频次或采购容量推断。有限直角图未给出路径时，只表示候选缺少合法路线证据，不宣称物理空间全局无解。

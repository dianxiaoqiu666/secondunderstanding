# 历史 M05 自主交付报告

当前是 [M06 真实问题修复与复验](M06_DELIVERY.md)。下文是已被用户产品验收拒绝的 M05 历史记录，其 PASS 字段不能作为当前产品验收结论。

**RESULT = PARTIAL；AUTONOMOUS FLOW ACCEPTANCE = PASS。** 正常完整输入已自动交付，少量缺口只补局部并自动继续，复用无需重复确认。唯一获批真实CAD仍缺空间事实，未伪造其完成。

| 报告字段 | 当前结果 |
|---|---|
| FAILED BASELINE PRESERVED | YES，3175b6a及_archive；M04强制审核也保留历史 |
| REFERENCE DESIGN PROFILE | PASS，已核验子集120实际模块/5图例，不冒充权威全店库存 |
| REFERENCE SHELF RUNS | 44严格排；36近共线诊断排 |
| STANDARD RECTANGLE ALGORITHM | PASS，六标准空间 |
| SHELFRUN MODEL / MIXED MODULE COMBINATION | PASS / PASS |
| SAME-RUN MODULE GAP / ISOLATED SHELF RATIO | 当前已测自动结果0mm / 0% |
| AISLE VALIDATION / OBSTACLE TESTS | PASS；柱、墙、孔洞、入口、凹形及排端/排间规则 |
| REFERENCE QUALITY COMPARISON | 独立结构覆盖项PASS；跨店物理密度/商业功能未建立可比条件，不声称整体商业最优 |
| REAL CAD HUMAN CONFIRMATION | 当前仅缺事实待补充，不是每份CAD必经审核 |
| REAL STORE-OUTSIDE SHELVES / REAL GEOMETRY VIOLATIONS | UNKNOWN / UNKNOWN，真实范围尚无有效事实 |
| CATEGORY PLACEMENT / SYNTHETIC SKU PLACEMENT | PASS / PASS |
| REAL SKU PLACEMENT | UNAVAILABLE-DUE-TO-MISSING-PHYSICAL-DATA |
| 2D COMPARISON | PASS：实际生成后诊断；不要求门店老板阶段审批 |
| 3D RESULT | 已完成测试图PASS；真实原CAD未生成 |
| MATERIAL REPORT / 3D MATERIAL COUNT MATCH | 已完成测试图JSON/XLSX与65实例PASS，445默认层；真实原CAD尚未交付 |
| HUMAN OPERATION BURDEN | 完整输入0、局部候选选择/提交2、同源复用0；真实缺事实样例总数UNKNOWN |
| SELF-OPTIMIZATION ITERATIONS | 已保留R01/M01/M01b/R02/M02/M03/M04/M05共8个修复与证据里程碑 |
| ACCEPTED ITERATIONS | 技术复核保留；不把研发通过当作真实门店已经人工确认 |
| REJECTED ITERATIONS | FAILED_BASELINE_01用户拒绝；M04强制逐图审核/阶段停机被本轮用户纠偏 |
| ABANDONED APPROACHES | 孤立架扫描、粗框自动闭合、仅靠指标元数据判安全、每任务五项审核/二维停机 |
| MATURE PACKAGES USED | ezdxf、Shapely/GEOS、Pydantic、FastAPI、uvicorn、openpyxl、SQLite、httpx、Three.js/OrbitControls、Playwright |
| CUSTOM PROJECT LOGIC | 连续排候选与特定长度DP、独立项目规则复核、品类配额、明确尺寸模拟分配、输入事实与服务协调 |
| AUTOMATED TESTS | 根最终337 passed +17 subtests，JUnit354、0失败/错误/跳过 |
| BROWSER E2E | 根实际完整输入/局部补充/复用到3D和下载PASS；真实原CAD安全报告缺事实PASS，但最终交付未完成 |
| SOURCE FILES MODIFIED | NO；37原件SHA/大小/修改时间不变 |
| LOCAL COMMITS | codex/shelfrun-repair本地主题里程碑，M05提交号见git log |
| PUSH | NO |
| WORKTREE | 提交后以最终git status --short核对，依赖/缓存/数据库/产物按既有忽略策略管理 |
| PROJECT RESULT | PARTIAL：剩余真实输入事实缺失，不是代码、许可证或逐阶段审批阻塞 |

## 使用

项目根目录一条命令：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

唯一网页 http://127.0.0.1:8100。交付时三个服务已启动，健康检查全部通过，负责人再次实际浏览器恢复已完成结果和真实待补充页面通过。完整输入上传后等待结果即可。要查看已实测的自动交付结果（明确合成测试图）：http://127.0.0.1:8100/?job=a59c8eb6442842f5b8d63aae4cf9b22f 。

当前真实CAD已准备页面：http://127.0.0.1:8100/?job=d2d96dfbeeac44ac94dae6f4432fc4ad 。仅需集中提供真实边界和入口，并选择图层0源图元的实际语义。现有粗框没有可信局部候选，程序不能替人猜缺失事实。没有改动墙或禁放区的全面重新审核要求。

**NEXT REQUIRED HUMAN ACTION = 只在上述真实图纸确实缺失的三项事实中补充必要信息。提交后系统自动交付3D和物料，无需回聊天回复、逐阶段审批或下指令继续。** 不要将合成测试图作为真实门店答案。

## 证据与已知限制

ACCEPTANCE.md、AUTONOMOUS_AUDIT.md、AUTOMATIC_INPUT.md、M05_STRUCTURAL_COMPARISON.md与ITERATION_LOG.md包含实际测试和来源。产物在outputs/acceptance_m05；当前源码/测试/说明与选定摘要和截图纳入本地版本，不提交运行密钥、数据库、上传原件或完整商品明细。

当前只支持安全二维毫米DXF子集；规则是局部排间/排端几何，不是全店可达/消防认证。0/90度全局候选与配额式品类分配不保证商业全局最优。参考真实面积未知、窄过道规则不同，不能靠数量/密度相近强造质量通过。真实SKU无包装尺寸，不伪造Facing/容量；货架高1800、墙高2600/厚100mm仅显示假设，物料清单不发明价格/库存/完整零件BOM。未提交绘图草稿不持久化；有效补充缓存由源/业务/规则/政策绑定，相关资料变更后必须重新验证。

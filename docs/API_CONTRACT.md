# 当前自动交付接口

## M09 明确空间算法输入

S3 `GET /algorithm` 是独立于 CAD 准备的算法验证页面。`GET /api/algorithm/scenarios` 返回冻结的明确空间输入；`POST /api/algorithm/scenarios/{id}/generate` 自动调用现有 S2 `POST /plan-explicit`，校验来源与物料一致性后返回前后二维布局数据。S1 不参与此路径。

S2 接受 `ExplicitPlanningInput`（`shared/explicit_planning.py`）：必填 `input_kind=ALGORITHM_VALIDATION`、名称、明确毫米空间（边界/孔洞、线性障碍、禁放区、通行保留带）、模板及规则。此入口不接受 CAD 来源、人工确认或未解决事实，不读取确认缓存；内部身份为已有 `SYNTHETIC_TEST`，不签名、不升为真实 CAD READY。原 `/plan-v2` 签名门槛不变。

返回 `input`、`input_sha256`、`before/after`（`layout`、`measurements`、`elapsed_ms`）及确定性 `selection`。旧算法找不到可发布方案时 `before.status=NOT_FOUND`、`layout=null`，不阻断仍合法的 after；当前有限搜索均未找到时返回422，明确不代表全局无解。S3只在输出生成后增加可选 `reference` 画像对照；参考密度不计算。详见 [M09固定口径](PLANNING_M09.md)。

## M08 外围工作与整店资格

准备/预览增加 `local_faces`（局部闭合结构，不可作为整店选项）、`perimeter_candidates`（可包含OPEN工作线组）、`gap_candidates` 和 `scope_ready`。每个外围线组分别提供原始 `source_edge_ids` 与 `logical_edge_ids`；派生段的 `sources` 保留原实体及源参数区间。`scope_candidates` 仅列通过整店资格判断的闭合外围；没有资格时不回退局部面。

预览、应用和最终提交接受 `perimeter_edge_ids`、`scope_candidate_id`、`scope_edge_ids`、`supplemental_edges` 与 `portal_closure_ids`。未提交的范围工作字段复用适用的已保存值；显式空数组清除对应集合，合法的 `scope_candidate_id:null` 清除候选选择，不能在S3序列化或后台转发时过滤掉。`valid=true`可以只表示局部工作有效；`scope_ready=false`时范围必须为空，不允许规划。所有必要问题消除且完整标准输入通过验证后，`ready=true`才自动继续。

外围工作按源/业务/规则/解析政策绑定保存，附独立范围政策版本并重验。范围工作失效或撤销不清除无关墙事实；逻辑闭合不新增Barrier。源孔洞、禁放结构仍保留，未知房间不自动作为孔洞。已有明确Planning Scope与已有设计查看沿原入口处理。

## M06 增量接口与两种用途

S3 `POST /api/v2/jobs` 使用 multipart `file` 和 `task_purpose`（默认 `generate`，或 `existing_design`）。生成用途保持生产准入与原规划算法；查看用途允许本次主动上传参考 CAD，保留原分类，S1 `POST /existing-design` 提取本次上传字节，S2 `POST /existing-design/check` 验证 `existing-1.0` 观察合同与清单一致性，不调用布局算法。S3 显示原位置并提供原设计 JSON 与观察清单 XLSX；缺范围不造地面，图例/未知对象不计入货架，未知层数/高度保持未知。查看用途没有规划前置确认，也不开放新规划的生成后参考对照。

S1 `POST /preview-input`、`POST /apply-input` 对应 S3 `/api/v2/jobs/{id}/preview-input`、`/apply-input`。接受 `resolutions`、可选 `boundary/entrances/exclusions`、`scope_edge_ids`、`supplemental_edges`、`revoked_issue_ids`及上述M08工作字段；S3绑定prepared_id。返回 `valid/ready/issues/resolved_issue_ids/candidate_space/feedback/source_usage/confirm_payload`。预览不写确认；应用保存通过局部校验的源事实及外围工作，未完成外围不会发布为规划区域。范围、孔洞、入口及固定约束通过完整验证后才产生可继续规划的标准输入。

原线有稳定 edge_id 及源实体/原始和世界坐标。范围组成、真实墙体和补充范围边分别记录；补充边不得自动变墙。撤销仅撤销指定有效局部事实，保持其他事实；完整缓存绑定局部事实版本。缓存按源字节、业务、规则和解析政策绑定并重验。公开准备包含已应用事实；旧待补充任务在读取时按当前准备合同刷新，不丢失仍有效的局部事实。`ready=true` 后网页自动提交并继续，不增加阶段审批。

完整增量合同见 [M06_REPAIR_CONTRACT.md](M06_REPAIR_CONTRACT.md)。以下生成接口仍保留兼容。

- S1 8101：POST /prepare（multipart file，仅CAD）；GET /prepared/{id}；POST /resolve（已有局部候选）；POST /confirm（必要缺失几何）。
- S2 8102：POST /plan-v2，输入PlanRequest，须通过本地签名及独立几何/业务验证，返回VALIDATED的LayoutV2。
- S3 8100：GET /；POST /api/v2/jobs；GET /api/v2/jobs/{id}；POST /api/v2/jobs/{id}/resolve 或 /confirm。
- 最终下载：GET /api/v2/jobs/{id}/layout.json、/materials.xlsx；preview.json是布局下载兼容别名。
- 生成后的可选对照：GET /api/v2/jobs/{id}/comparison、/reference.svg。参考不会传入S1/S2。
- 旧 /understand、/plan、/api/jobs 返回410，不自动调用failed_baseline_app。

/prepare 返回prepared_id/source/candidate_space/business_summary/evidence/drawing，加status、readiness、issues和plan_request。READY必须带完整标准包，S3自动送规划；REQUIRES_INPUT只列缺失事实。S3公开prepared去除包含完整业务的plan_request。

/resolve体为resolutions:[{issue_id,option_id}]及严格JSON布尔user_confirmed:true。用户只提交服务器已展示的候选ID，S3绑定prepared_id。/confirm允许仅传必要boundary/entrances/exclusions、resolutions、reviewed_fields和user_confirmed；省略已知几何，不强制五项审核。固定墙/孔洞/禁放/入口不能被删除或改写。

Confirmation.state=AUTO_VALIDATED表示明确源事实自动验证，CONFIRMED表示补充了必要事实；SYNTHETIC_TEST生产接口拒绝。签名绑定源、几何、完整业务、规则、状态、证据、操作者和审核字段。补充缓存还绑定政策版本，复用重新严格验证。

任务状态queued/preparing/planning自动流转至complete；仅缺事实时awaiting_confirmation，技术失败为failed。补充提交后自动恢复全流程，不存在正常任务的二维人工审批停点。共享模型见shared/planning_v2.py，JSON Schema见shared/schemas。

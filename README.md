# secondunderstanding · 门店货架设计

本项目来自 `C:/D/汉斯/平面与货架-L3`。2026-09-11 已在原有货架算法快照上补入设计页面、导出组件、回归测试、模板、场景、参考对照及运行支持。当前使用本目录的独立 Python 环境，默认只启动规划服务和设计页面。

GitHub 仓库：[dianxiaoqiu666/secondunderstanding](https://github.com/dianxiaoqiu666/secondunderstanding)，可见性为私有。本机原目录名 `secondunderstand` 保留。

## 从 GitHub 获取

Windows 下准备 Python 3.12 后执行：

```powershell
git clone -c core.autocrlf=false https://github.com/dianxiaoqiu666/secondunderstanding.git
Set-Location -LiteralPath .\secondunderstanding
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

克隆时保留原字节和换行，便于核对冻结数据的 SHA256。每台机器通过准备脚本建立自己的 `.venv`。模板、冻结回归输入及参考诊断数据随源码提交。

## 本机启动与停止

```powershell
Set-Location -LiteralPath 'C:\D\汉斯\secondunderstand'
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

打开 [货架与作业设计页面](http://127.0.0.1:8130/algorithm)，选择场景并点击“生成并比较”。规划 API 为 [接口文档](http://127.0.0.1:8132/docs)。默认端口为 8130/8132；端口已占用时启动会退出，不会停止其他项目。

在启动窗口按 Ctrl+C，或在另一终端运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File 'C:\D\汉斯\secondunderstand\stop.ps1'
```

停止脚本校验本项目路径、进程身份和启动时间，只向已验证的本项目 supervisor 发出停止请求。

自定义端口示例：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1 -UiPort 8140 -PlanningPort 8142
```

## 补齐后的内容

| 内容 | 本项目位置与用途 |
|---|---|
| 连续排、单双面、背靠背与路线算法 | [services/planning](https://github.com/dianxiaoqiu666/secondunderstanding/tree/main/services/planning)；原有 35 份复制文件保留 |
| 设计页面与导出 | [services/delivery](https://github.com/dianxiaoqiu666/secondunderstanding/tree/main/services/delivery)；二维比较、作业证据、3D 查看器源码、JSON/Excel 导出源码、本地 Three.js 与许可证 |
| 场景 | [tools/benchmarks](https://github.com/dianxiaoqiu666/secondunderstanding/tree/main/tools/benchmarks)；17 个作业样例及 7 个纯几何样例 |
| 六种货架模板 | [material_templates.v1.json](https://github.com/dianxiaoqiu666/secondunderstanding/blob/main/input/production/materials/material_templates.v1.json)；规划默认值，不代表库存和采购规格已确认 |
| 测试 | [tests](https://github.com/dianxiaoqiu666/secondunderstanding/tree/main/tests)；规划、产品、独立审计、交付、参考、界面与本地启动安全 |
| 冻结数据 | `outputs/planning_m09/before`、`outputs/iterations/M01`、`outputs/baselines/SYNTHETIC_BASELINE`；旧合法方案回归输入，不覆盖 |
| 参考对照 | [tools/reference_profile](https://github.com/dianxiaoqiu666/secondunderstanding/tree/main/tools/reference_profile)、`outputs/reference_profile`；生成后的独立诊断，不作为规划输入 |
| 旧版 S1 兼容源码 | [services/understanding](https://github.com/dianxiaoqiu666/secondunderstanding/tree/main/services/understanding)；保留原回归测试的导入依赖，默认不启动 |
| 原数据分类清单 | [DATA_CLASSIFICATION.md](https://github.com/dianxiaoqiu666/secondunderstanding/blob/main/DATA_CLASSIFICATION.md)；兼容解析器用其中的参考文件哈希拒绝误用，文档里的路径和授权为源项目历史记录 |

算法页使用的是明确标示的合成测试场景。正式 `/plan-v2` 仍要求有效的标准包和签名；新 CADunderstanding 的 `CadPackage` 适配、真实 CAD 联调、真实商品容量和采购结构尚未完成。主站 `/` 保留 L3 CAD 工作台源码，它需要另外配置兼容的 S1 服务；请使用 `/algorithm` 作为当前可独立运行的入口。

3D 查看器和 Excel 导出功能的代码及资源已经补回，但完整的“上传真实 CAD → 设计 → 3D/清单”流程仍取决于上游数据与契约。程序的几何/路线 PASS 与真实门店人工验收分别记录。

## 环境与测试

本机 `.venv` 已通过重建虚拟环境和校验复制方式补齐 35 项锁定依赖，另由新虚拟环境提供 pip，未复制源环境启动器、缓存或密钥。迁移环境使用与源环境相同的 CPython 3.12 Windows ABI。可验证：

```powershell
.\.venv\Scripts\python.exe -B -m pip check
.\.venv\Scripts\python.exe -B -m pytest -q -m 'not browser'
```

原有真实商品表断言在商品表未迁移时明确跳过；原 L3 8100–8102 三服务浏览器验收默认跳过。其余原测试保持原断言，不使用合成商品冒充真实商品表。独立的签名完整性测试使用临时测试密钥。

界面组件测试可使用已安装的 Edge：

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q tests/planning_m10/test_ui.py tests/planning_m11/test_ui.py --basetemp=.tmp/pytest-ui --browser-executable 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
```

其他机器需要重建环境时，可使用 `scripts/setup.ps1` 安装锁定依赖；也可指定同 ABI 的源项目 `-SourceProject`，从其现有环境做校验复制。`-WithBrowser` 会安装本项目的 Playwright Chromium，正常使用网页无需安装 Node/npm。

## 迁移记录

本轮从 L3 新增 147 个文件，原 35 个文件字节不变；另新增本项目的启动、环境准备、测试配置和迁移记录。2026-09-11 最终回归为 395 项测试及 51 项子测试通过、12 项明确跳过；10 项浏览器组件测试另行通过。真实本地服务的合成场景生成、任务切换和货架排查看已验证；测试服务已停止。

- [本轮补充清单](https://github.com/dianxiaoqiu666/secondunderstanding/blob/main/docs/migration/2026-09-11-L3-supplement-manifest.json)：逐文件来源、用途、大小、SHA256 和源修改时间。
- [复制校验](https://github.com/dianxiaoqiu666/secondunderstanding/blob/main/docs/migration/2026-09-11-L3-supplement-verification.json)：目标一致性及源目录保护结果。
- [运行依赖清单](https://github.com/dianxiaoqiu666/secondunderstanding/blob/main/docs/migration/2026-09-11-runtime-manifest.json)。
- [迁移与验收记录](https://github.com/dianxiaoqiu666/secondunderstanding/blob/main/docs/migration/2026-09-11-补充迁移记录.md)。

原始 `COPY_INFO.md`、`COPY_MANIFEST.json` 和早先的探索方案保留为当时快照。迁入的 L3 文档保留其历史状态和路径，不能视为本项目当前的验收报告或执行授权。原三服务启动/配置文件放在 `_archive/l3-support-20260911`，当前启动与测试以本 README 为准。

本轮未迁入真实 CAD、真实商品工作簿、已有运行数据库、Human 确认状态、确认密钥、源 Git 历史或 `node_modules`。本目录已初始化独立 Git 仓库；开发环境、缓存与运行状态由 `.gitignore` 保留在本机，必要的冻结数据和合成测试证据采用明确文件清单提交。

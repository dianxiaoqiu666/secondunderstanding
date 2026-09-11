# L3 数据仓库分类清单

## 范围与保护结论

- 原始仓库副本：`数据仓库/`（RAW WAREHOUSE COPY）；本清单只盘点并从中复制精选文件，绝不移动、重命名、转换或改写原件。
- 精选根目录：`input/`。项目已经采用单数目录名，因此没有另建重复的 `inputs/`。
- 盘点对象：29 个原始文件；分类日期：2026-09-08。
- `material_templates.v1.json`：已从 Human 批准的只读源以字节复制方式导入，并已核验 SHA256、大小和修改时间；没有复制源目录中的任何其他文件。
- 精选副本均是字节级复制。其源与目的 SHA256 已核验一致。
- 重复文件：未发现 SHA256 相同的原始文件组。

分类规则：明确授权的主 CAD 和商品主数据为 `PRODUCTION_INPUT`；物料模板只有在文件存在且批准时才是 `MATERIAL_CONFIG`；历史设计、布局和数据库为 `REFERENCE_ONLY`；手册、README、模型和 schema 为 `SUPPORTING_REFERENCE`。没有证据的资料才进入 `UNKNOWN`。

## 汇总

- PRODUCTION INPUT COUNT = 2
- MATERIAL CONFIG COUNT = 1
- REFERENCE ONLY COUNT = 18
- SUPPORTING REFERENCE COUNT = 9
- UNKNOWN COUNT = 0

## Production Inputs

| 文件名 | 当前相对路径 | SHA256 | 文件类型 | 分类 | 精选副本 | 简短用途 |
| --- | --- | --- | --- | --- | --- | --- |
| 只留墙体之人工画框.dxf | `数据仓库/01-原始工程资料/store_001/只留墙体之人工画框.dxf` | `bd92b40c74cbbe75001b470f20afbaa3ca4aa3da0d2cb425d98d8c1d015defd0` | DXF | PRODUCTION_INPUT | 是：`input/production/cad/只留墙体之人工画框.dxf` | PRIMARY PLANNING CAD；仅此精确文件可作正式规划 CAD。 |
| 副本门店商品导出_1000370158187220260814.xlsx | `数据仓库/02-门店业务数据/副本门店商品导出_1000370158187220260814.xlsx` | `e66bdeed9321ce29e569267a059bcb6d0f7f17cc53af1037b0f61da5c24cb169` | XLSX | PRODUCTION_INPUT | 是：`input/production/products/副本门店商品导出_1000370158187220260814.xlsx` | PRODUCT MASTER DATA。 |
| material_templates.v1.json | `input/production/materials/material_templates.v1.json` | `b63863780526fefcdb35f9cde07673ed538b287251da77b80e8b69612a4cb024` | JSON | MATERIAL_CONFIG | 是：`input/production/materials/material_templates.v1.json` | AUTHORITY = HUMAN_APPROVED_RAPID_DESIGN_CONFIG；6 种获批货架模板，默认层数 6 / 6 / 4 / 5 / 7 / 7；用于正式规划，不是库存、供应商或采购目录。 |

## Reference Only

| 文件名 | 当前相对路径 | SHA256 | 文件类型 | 分类 | 精选副本 | 简短用途 |
| --- | --- | --- | --- | --- | --- | --- |
| #最终选定规划.dxf | `数据仓库/01-原始工程资料/store_001/#最终选定规划.dxf` | `fa754f36dcf7c7e3879938f83d43cef8597edba416764f9bbdaba1e0890ccb3a` | DXF | REFERENCE_ONLY | 否 | 命名变体的历史规划 CAD，仅供比较。 |
| 门店设计_单份数据提取.json | `数据仓库/01-原始工程资料/store_001/门店设计_单份数据提取.json` | `1223af6d00b173e5bb2aa9a567f997c4801ca6870a6bac390ef70ef9ccc21339` | JSON | REFERENCE_ONLY | 否 | 历史门店设计提取结果。 |
| 门店设计布局原始资料_预览.png | `数据仓库/01-原始工程资料/store_001/门店设计布局原始资料_预览.png` | `fe4db1aa05e31eb9b44451f23f69523c66b93887dd58d9ca572add6a13324365` | PNG | REFERENCE_ONLY | 否 | 原始工程资料预览图。 |
| 门店设计布局原始资料_预览.svg | `数据仓库/01-原始工程资料/store_001/门店设计布局原始资料_预览.svg` | `5bd9deb2105713456b48ecfdeed3357878a08e6f18f22d024f72053c961f9e39` | SVG | REFERENCE_ONLY | 否 | 原始工程资料预览图。 |
| 门店设计布局原始资料.dwg | `数据仓库/01-原始工程资料/store_001/门店设计布局原始资料.dwg` | `fd42a0b698d21078fa315d8423985480ca7d7b09e3a58772ddf09907afc56f8c` | DWG | REFERENCE_ONLY | 否 | 原始工程 CAD，不是授权的主规划输入。 |
| 门店设计布局原始资料.dxf | `数据仓库/01-原始工程资料/store_001/门店设计布局原始资料.dxf` | `df87c34fb446b0c019b1b232f2d8ed7ee6c2b1d19f7587c762befc093241b831` | DXF | REFERENCE_ONLY | 否 | 原始工程 CAD 的交换格式。 |
| 门店设计图.jpg | `数据仓库/01-原始工程资料/store_001/门店设计图.jpg` | `ec63207243bb5eb26f3dcdf3d0c44c934e52831f5fb7be1953948ca3cb9c2240` | JPG | REFERENCE_ONLY | 否 | 历史门店设计图。 |
| 选定规划只留墙体_数据提取.json | `数据仓库/01-原始工程资料/store_001/选定规划只留墙体_数据提取.json` | `b63aa92ebc8fdbbc136b0bab0fb5c57872f024e5c17fbd59bca838bbc8beae6e` | JSON | REFERENCE_ONLY | 否 | 历史 CAD 的提取结果。 |
| 选定规划只留墙体_预览.png | `数据仓库/01-原始工程资料/store_001/选定规划只留墙体_预览.png` | `768093cfb93d38fee14f5348673bbae8b629c666401aa072fe76c802aa98a36c` | PNG | REFERENCE_ONLY | 否 | 历史 CAD 的预览。 |
| 选定规划只留墙体.dxf | `数据仓库/01-原始工程资料/store_001/选定规划只留墙体.dxf` | `0753b00fae27eaa1355b9a3274789079ad9d2b33b28ba343f9afe5516d91468d` | DXF | REFERENCE_ONLY | 否 | 相似但非授权精确文件名的 CAD 变体。 |
| 选定规划只留墙体.dxf~ | `数据仓库/01-原始工程资料/store_001/选定规划只留墙体.dxf~` | `1fefc17820c206da9c2a66be16527503778c1ab805d2727b4a74943d372a185d` | DXF backup | REFERENCE_ONLY | 否 | CAD 备份变体。 |
| 原始资料修改为只有一个门店设计，无多余内扩展.dxf | `数据仓库/01-原始工程资料/store_001/原始资料修改为只有一个门店设计，无多余内扩展.dxf` | `c19be5b57b39f7088a8ef749125cef206021d93fa0e858da537335489c4655e9` | DXF | REFERENCE_ONLY | 是：`input/reference/cad/原始资料修改为只有一个门店设计，无多余内扩展.dxf` | 成熟设计参考 CAD；仅用于开发后验证，不能参与生产布局、补齐主 CAD 边界或复制历史货架位置。 |
| 原始资料修改为只有一个门店设计，无多余内扩展.dxf~ | `数据仓库/01-原始工程资料/store_001/原始资料修改为只有一个门店设计，无多余内扩展.dxf~` | `74938d175da7dfa4ec096582a43e72a81dabe63f8205fa8f0563d5cdf67d888d` | DXF backup | REFERENCE_ONLY | 否 | 成熟设计 CAD 的备份变体。 |
| 最终选定规划.dxf | `数据仓库/01-原始工程资料/store_001/最终选定规划.dxf` | `61e861e1bb0466b4761eeab0bf866d5af8952b301ccbf6f6f81671655bb1a52e` | DXF | REFERENCE_ONLY | 否 | 历史最终规划 CAD，非授权主 CAD。 |
| 最终选定规划.dxf~ | `数据仓库/01-原始工程资料/store_001/最终选定规划.dxf~` | `da62f410d71de974c3cdce66c6f46813e743fc823fc05980e0d98c56444e74d5` | DXF backup | REFERENCE_ONLY | 否 | 历史最终规划的备份变体。 |
| real_store_floor_layout.json | `数据仓库/02-门店业务数据/real_store_floor_layout.json` | `0b24f1f8524509551371b98a7e9b7b24434ffe9cfd0b367eaf096802b8d93c10` | JSON | REFERENCE_ONLY | 否 | 历史门店平面布局结果。 |
| real_store_layout_draft.json | `数据仓库/02-门店业务数据/real_store_layout_draft.json` | `3836195b9848fe448d94e63a40970a3516d18e707fae71a0cb2a16e74c49f6b8` | JSON | REFERENCE_ONLY | 否 | 历史布局草案。 |
| v2_data_备份_20260902.sqlite | `数据仓库/03-数据库/v2_data_备份_20260902.sqlite` | `289b19ff20cf813de76757d061c1321f1194c64794089156d886db3928e94b4f` | SQLite | REFERENCE_ONLY | 否 | V2 数据库备份；无当前正式配置源证据。 |

## Supporting Reference

| 文件名 | 当前相对路径 | SHA256 | 文件类型 | 分类 | 精选副本 | 简短用途 |
| --- | --- | --- | --- | --- | --- | --- |
| 门店设计_单份数据提取.md | `数据仓库/01-原始工程资料/store_001/门店设计_单份数据提取.md` | `6938c161def928dcb2a2caa8fe1932f6391e58da1278100e452ee5263143dfab` | Markdown | SUPPORTING_REFERENCE | 否 | 门店设计提取说明。 |
| README.md | `数据仓库/01-原始工程资料/store_001/README.md` | `86c9f3fb6968083a18cff1b265e93567fac9f97943b657a7f688eb55c19bc29c` | Markdown | SUPPORTING_REFERENCE | 否 | 原始工程资料说明。 |
| 04-物料数据来源-桂双百培训手册摘要.md | `数据仓库/02-门店业务数据/04-物料数据来源-桂双百培训手册摘要.md` | `3887f442499a1e6d4ac8584c6e1383141773e26b5dc5842b9d9e0e348d3eae7c` | Markdown | SUPPORTING_REFERENCE | 否 | 培训手册摘要，不自动成为物料配置。 |
| 桂双百便利超市培训手册3(2).docx | `数据仓库/02-门店业务数据/桂双百便利超市培训手册3(2).docx` | `179879203f156825bdff3a6e9e91949530de648682bbff5409a13f1d49b825ec` | DOCX | SUPPORTING_REFERENCE | 否 | 培训手册。 |
| floor_layout_schema.json | `数据仓库/02-门店业务数据/floor_layout_schema.json` | `0d4ea30b11eba9182738019fa301e4c83eaaeff857fc3eeeccede63bb4a74f39` | JSON schema | SUPPORTING_REFERENCE | 否 | 平面布局数据模型参考。 |
| MATERIAL-DATA-MODEL-v0.md | `数据仓库/02-门店业务数据/MATERIAL-DATA-MODEL-v0.md` | `69b786df194843815fededd3381985dea5665b256d9cf9cf3ca322aa2e3667fc` | Markdown | SUPPORTING_REFERENCE | 否 | 物料数据模型说明。 |
| PRODUCT-DATA-MODEL-v0.md | `数据仓库/02-门店业务数据/PRODUCT-DATA-MODEL-v0.md` | `fbb725a54d812ddb24a79d4c0f2cdd666316243e94f9a8c861f420a5a707bb7b` | Markdown | SUPPORTING_REFERENCE | 否 | 商品数据模型说明。 |
| shelf_layout_schema.json | `数据仓库/02-门店业务数据/shelf_layout_schema.json` | `5767fb1e34c5a94161c3c05352d9951066601dec6f46db8005a6fa9976ad91b6` | JSON schema | SUPPORTING_REFERENCE | 否 | 货架布局数据模型参考。 |
| README.md | `数据仓库/README.md` | `9019bc1853cf39f2c49f603594193368af7e93536fc3fc1a568a216f49f59b85` | Markdown | SUPPORTING_REFERENCE | 否 | 数据仓库目录说明。 |

## Unknown

无。29 个原始文件均能依照本工作包的文件类别和明确文件名规则归类；本轮没有把任何文件因名称相似而提升为生产输入。

## 精选区与 Git 保护

- 已确保目录：`input/production/cad/`、`input/production/products/`、`input/production/materials/`、`input/reference/cad/`、`input/reference/documents/`。
- `input/production/materials/material_templates.v1.json` 是唯一已批准的物料配置；不以培训资料、参考 JSON、库存、供应商或采购目录替代。
- `.gitignore` 已忽略 `数据仓库/` 及 `input/` 下的精选数据，数据文件不应被 Git 跟踪。
- `DATA_CLASSIFICATION.md` 是唯一新增的分类清单；原始文件零删除、零重命名、零改写。

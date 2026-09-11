# 第二阶段：货架规划复制记录

复制时间（UTC）：2026-09-10T02:20:26.149833+00:00

来源：C:\D\汉斯\平面与货架-L3

目标：C:\D\汉斯\secondunderstand

来源分支：codex/shelfrun-repair；HEAD：4ba98c58f98dff722194ff435b831fde7e54f21a。

复制当前工作区文件，包括尚未提交的现有修改；保留原有目录结构和文件字节。

包含 services/planning、shared 共享模型及 Schema、Python 依赖锁文件。

共复制 35 份源文件。COPY_MANIFEST.json 逐项记录相对路径、大小和 SHA256。

本次仅完成源码与必要共享依赖的复制和一致性校验。未迁移 .venv、node_modules、缓存、Git 历史、真实输入、运行数据库、确认密钥及 Human 状态；未安装依赖或启动服务。原项目内容保持不变。

独立运行与三个目录之间的联调尚未配置、验证。原入口仍为 services.planning.app:app，运行前需准备目标目录的 Python 环境。生产规划仍要求原契约兼容的 S1 标准包与签名；网页仍通过 HTTP 调用 S1/S2。第一阶段 CADunderstanding 的新 CAD 契约接入及签名配置不属于本次复制，不能将本目录直接视为已经完成联调的独立系统。

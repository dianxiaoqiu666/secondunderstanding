# 微服务3：自动交付网页

唯一入口 http://127.0.0.1:8100。独立启动：.venv/Scripts/python.exe -B -m uvicorn services.delivery.app:app --host 127.0.0.1 --port 8100。

上传→S1准备→若READY自动S2规划→实例/清单/摘要检查→XLSX生成→complete。只在缺事实时显示局部补充；提交后自动继续。三维基于同一LayoutV2的space/runs/shelves/bom，采用本地Three.js和OrbitControls。结果可下载JSON/XLSX。

状态库runtime/workbench/jobs.sqlite3，标准接口/API及恢复说明见根docs/API_CONTRACT.md、OPERATIONS.md。旧端点410，failed_baseline_app仅历史回归。最终验收证据见docs/ACCEPTANCE.md。

# 微服务1：明确源事实与必要补充

独立启动：.venv/Scripts/python.exe -B -m uvicorn services.understanding.app:app --host 127.0.0.1 --port 8101。

POST /prepare读取DXF并加载批准内部业务。完整明确几何自动验证并输出READY标准包；只有真实缺失事实才输出issues，不要求五项全面审核。明确入口图层ENTRANCE/ENTRANCES/入口纳入固定约束。

POST /resolve接受源局部候选ID，POST /confirm接受必要几何补充；已知事实可省略。保持固定墙、显式孔洞、禁放区和入口，严格验证有效几何，签名绑定源/空间/业务/规则/状态/证据。后续同源同配置重新验证并复用缓存。

生产真实粗框不自动闭合、修复或当边界。危险几何明确拒绝，不读取成熟参考补答案。缓存与上传在runtime/understanding_v2；本地签名密钥不入Git。详细契约/局部阈值/缓存限制见根docs/AUTOMATIC_INPUT.md。

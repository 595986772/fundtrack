"""项目启动入口。

运行 `fastapi dev main.py` 时，FastAPI CLI 会在这个文件里寻找名为 `app`
的 FastAPI 应用对象。真正的应用创建逻辑放在 `app/main.py`，这里保持很薄，
方便你以后把项目拆得更清楚。
"""

# 从 app.main 导入已经创建好的 FastAPI 实例。
# 这样外部命令依然可以使用 `main:app` 或 `fastapi dev main.py` 启动项目。
from app.main import app

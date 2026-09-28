"""pytest 全局夹具：把数据落点指向临时目录，避免测试污染开发用的 data/。

覆盖两件事，缺一不可：

1. **把 `python-backend/` 放进 sys.path**。裸 `pytest` 与 `python -m pytest` 的
   `sys.path[0]` 不同：前者是 Scripts/bin 目录（不含 cwd），后者才是 cwd。
   `tests/*.py` 要 `from main import app`，于是裸 `pytest` 会在**收集期**就
   `ModuleNotFoundError: No module named 'main'`，三个测试文件全报 ERROR，
   pytest 以退出码 2 中断。CI 首次运行（run #1）正是这么挂的。这里显式插入，
   使两种调用方式都可用，不再依赖「恰好用了 python -m」。

2. **把四个数据落点指向临时目录**：
   - 三个 SQLite 库（knowledge / auth / orders）；
   - RAG 向量索引目录——`test_rag_retrieval_smoke` 会调 `load_pipeline()`，若不加这层，
     它会在 `data/chroma_rag/` 里用「只有 5 行 demo 的临时知识库」重建索引
     （指纹不匹配即触发全量重建），把开发用的 1328 chunk 索引打回 28 个。

为什么必须在 conftest 顶层、且早于 `import main` 设置环境变量：
knowledge_store / auth / order_store / rag.indexer 都在**模块导入时**就把路径固化成常量
（`DB_PATH = Path(os.getenv(...) or 默认路径)`、`DEFAULT_PERSIST_DIR = ...`）。一旦 main
被导入——哪怕只是 `from main import app`——路径就已定型，之后再设环境变量不会生效。
conftest.py 由 pytest 在收集测试模块之前导入，因此这里是唯一可靠的时机。

用 setdefault 而不是直接赋值：显式传入的环境变量优先，便于 CI 或本地调试覆盖。
"""
import os
import sys
import tempfile
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="aics_test_"))

os.environ.setdefault("KNOWLEDGE_DB_PATH", str(_TMP_DIR / "knowledge.db"))
os.environ.setdefault("AUTH_DB_PATH", str(_TMP_DIR / "auth.db"))
os.environ.setdefault("ORDER_DB_PATH", str(_TMP_DIR / "orders.db"))
os.environ.setdefault("RAG_PERSIST_DIR", str(_TMP_DIR / "chroma_rag"))

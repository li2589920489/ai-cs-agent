"""smoke 测试 — /health + 鉴权（A 系列）+ Triage 路由 + RAG 检索

- /health、鉴权：无外部依赖，CI 与本地都能跑
- Triage 路由：需要 LLM API key（无 key 时自动 skip）
- RAG 检索：需要本地 BGE 模型（无模型时自动 skip）
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

# 加载 .env（确保 OPENAI_API_KEY 进入 os.environ，供 skipif 判断与 LLM 调用）
load_dotenv()

from main import app  # noqa: E402

client = TestClient(app)

_HAS_API_KEY = bool(os.getenv("OPENAI_API_KEY"))
_BACKEND_DIR = Path(__file__).resolve().parent.parent
# 两个条件都要满足才能跑 RAG 用例：模型文件在 + sentence-transformers 装得上。
# 只判模型目录会漏掉「装了模型但没装依赖」的环境——那种情况下 Embedder 会抛
# RuntimeError（无法初始化 Embedding），是报错而不是 skip。
_HAS_MODEL = (_BACKEND_DIR / "data" / "models" / "bge-small-zh-v1.5" / "config.json").exists()
_HAS_ST = importlib.util.find_spec("sentence_transformers") is not None
_HAS_RAG = _HAS_MODEL and _HAS_ST

# 鉴权测试用凭据
TENANT_A = ("tenant_a", "key_a_123", "租户A", "tenant")
TENANT_B = ("tenant_b", "key_b_456", "租户B", "tenant")
AGENT = ("agent", "agent_key_789", "坐席", "agent")


@pytest.fixture(scope="module", autouse=True)
def _seed_tenants():
    """seed 测试凭据（幂等）。

    必须用 upsert_tenant 而不是 create_tenant：main.py 在导入时会执行
    auth.ensure_seed_tenants()，其中已经把 `agent` 租户按环境变量建了出来。
    此时 create_tenant 因 tenant_id 冲突返回 False 且**不覆盖**，测试用的
    agent_key_789 根本没入库 → 坐席接口用例拿到 401 假失败。

    upsert_tenant 则会把 agent 的凭据改写为测试值，保证用例自洽。
    """
    import auth

    auth.upsert_tenant(*TENANT_A)
    auth.upsert_tenant(*TENANT_B)
    auth.upsert_tenant(*AGENT)
    yield


# ==================== E1：/health ====================

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["agents"] == "6"


# ==================== A 系列鉴权验证 ====================

def test_knowledge_get_without_key_allowed():
    """A5：无 key 走 default 租户，GET 放行"""
    r = client.get("/api/knowledge")
    assert r.status_code == 200


def test_knowledge_write_without_key_denied():
    """A5：无 key 写操作拒绝（403）"""
    r = client.post("/api/knowledge", json={"name": "越权商品", "product_id": "T-XXX"})
    assert r.status_code == 403


def test_invalid_key_401():
    """A2：非法 key → 401"""
    r = client.get("/api/knowledge", headers={"x-api-key": "invalid-key"})
    assert r.status_code == 401


def test_valid_key_200():
    """A2：合法 key → 对应租户，200"""
    r = client.get("/api/knowledge", headers={"x-api-key": "key_a_123"})
    assert r.status_code == 200


def test_tenant_isolation():
    """A3：租户隔离 — A 的 key 无法看到 B 的数据"""
    # 给 B 租户写入一条独特数据
    client.post(
        "/api/knowledge",
        json={"name": "B租户专属商品", "product_id": "B-ISOLATION-001"},
        headers={"x-api-key": "key_b_456"},
    )
    # 用 A 的 key 查询，不应出现 B 的数据
    r = client.get("/api/knowledge", headers={"x-api-key": "key_a_123"})
    assert r.status_code == 200
    names = [item["name"] for item in r.json()["items"]]
    assert "B租户专属商品" not in names


def test_agent_endpoint_requires_role():
    """A4：坐席接口需坐席角色 — 无 key 401 / 普通 key 403 / 坐席 key 通过"""
    # 无 key → 401
    r1 = client.post("/api/escalations/TKT-NOTEXIST/accept")
    assert r1.status_code == 401
    # 普通租户 key → 403
    r2 = client.post(
        "/api/escalations/TKT-NOTEXIST/accept", headers={"x-api-key": "key_a_123"}
    )
    assert r2.status_code == 403
    # 坐席 key → 通过（工单不存在，返回业务错误而非鉴权错误）
    r3 = client.post(
        "/api/escalations/TKT-NOTEXIST/accept", headers={"x-api-key": "agent_key_789"}
    )
    assert r3.status_code == 200
    assert "error" in r3.json()


# ==================== E1：Triage 路由 smoke ====================

@pytest.mark.skipif(not _HAS_API_KEY, reason="需要 LLM API key")
def test_triage_routing_smoke():
    """订单查询应路由到订单详情 Agent（不精确断言 final_agent，只验链路可用）"""
    r = client.post("/api/chat", json={"message": "帮我查一下订单的物流到哪了"})
    assert r.status_code == 200
    body = r.json()
    assert "final_agent" in body
    assert "reply" in body


# ==================== E1：RAG 检索 smoke ====================

@pytest.mark.skipif(not _HAS_RAG, reason="需要本地 BGE 模型 + sentence-transformers")
def test_rag_retrieval_smoke():
    """纯向量检索能召回结果（不加载 reranker，保持轻量）"""
    from rag import load_pipeline

    pipeline = load_pipeline()
    results = pipeline.search("坚果", k=3, use_hybrid=False, use_rerank=False)
    assert len(results) > 0


# ==================== E2：测试隔离（防回归） ====================

def test_rag_index_dir_is_isolated():
    """回归保护：RAG 索引目录必须被 conftest 指向临时目录。

    为什么值得单独立一条：上面那条 `test_rag_retrieval_smoke` 会调 `load_pipeline()`，
    而索引目录一旦落回 `python-backend/data/chroma_rag/`，测试就会用「只有 5 行 demo 的
    临时知识库」在那里重建索引（知识库指纹不匹配即触发全量重建），把开发用的 1328 chunk
    索引打回 28 个。已实测复现：`RAG_PERSIST_DIR=` 置空跑一次 pytest 即触发。

    本用例锁死这层隔离——若将来有人删掉 conftest 里的那行 setdefault，它会立刻变红。
    用 importorskip：CI 只装精简依赖，numpy 缺失时跳过而不是报错。
    """
    rag_indexer = pytest.importorskip("rag.indexer", reason="需要 numpy 等 RAG 依赖")

    persist_dir = str(rag_indexer.DEFAULT_PERSIST_DIR)
    assert "aics_test_" in persist_dir, (
        f"RAG 索引目录未被隔离，会污染开发用索引：{persist_dir}\n"
        f"修法：conftest.py 中 setdefault('RAG_PERSIST_DIR', str(_TMP_DIR / 'chroma_rag'))"
    )

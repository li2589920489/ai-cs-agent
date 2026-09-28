#!/usr/bin/env bash
# H1 鉴权端到端验证 — 覆盖坐席工作台（agent Key）与知识库面板（tenant Key）两条链路
#
# 前置：后端已启动
#   python -m uvicorn main:app --host 127.0.0.1 --port 8001
# 用法：
#   bash scripts/verify_h1_auth.sh [BASE_URL]
#
# 注：坐席接口对「工单不存在」返回 200 + {"error": "..."}（既有设计），
#     因此用例的判定点是「是否越过鉴权门」（401/403 vs 200），而非 404。

BASE="${1:-http://127.0.0.1:8001}"
TK="tk_default_iwTtjbZoLHo_JvFr2LJVMa_JMipw2pQ_"
AK="ak_agent_zZ1imaCgQLnt2NbN4om_GeoeWqnghUS1"
BAD="invalid_key_xxx"

BODY=".h1_body.txt"
TIMEOUT=120

pass=0
fail=0

# check <期望码> <说明> <curl 参数...>
check() {
  local want="$1"; shift
  local desc="$1"; shift
  local code
  code=$(curl -s --noproxy '*' -m "$TIMEOUT" -o "$BODY" -w '%{http_code}' "$@")
  if [ "$code" = "$want" ]; then
    printf '  OK   %-58s %s\n' "$desc" "$code"
    pass=$((pass+1))
  else
    printf '  FAIL %-58s 期望%s 实际%s\n' "$desc" "$want" "$code"
    printf '       body: %s\n' "$(head -c 160 "$BODY" 2>/dev/null)"
    fail=$((fail+1))
  fi
}

echo "=== 基址: $BASE ==="
echo ""
echo "--- A. 坐席工作台链路（require_agent_role）---"
check 401 "GET  /api/escalations               无 key"                     "$BASE/api/escalations"
check 401 "GET  /api/escalations               key 无效"                   -H "x-api-key: $BAD" "$BASE/api/escalations"
check 403 "GET  /api/escalations               用 tenant key（角色不足）" -H "x-api-key: $TK" "$BASE/api/escalations"
check 200 "GET  /api/escalations               用 agent key"              -H "x-api-key: $AK" "$BASE/api/escalations"

check 401 "GET  /api/escalations/T999/messages 无 key"                    "$BASE/api/escalations/T999/messages"
check 401 "GET  /api/escalations/T999/messages key 无效"                  -H "x-api-key: $BAD" "$BASE/api/escalations/T999/messages"
check 403 "GET  /api/escalations/T999/messages 用 tenant key"             -H "x-api-key: $TK" "$BASE/api/escalations/T999/messages"
check 200 "GET  /api/escalations/T999/messages 用 agent key（越门）"      -H "x-api-key: $AK" "$BASE/api/escalations/T999/messages"

check 401 "POST /api/escalations/T999/accept   无 key"                    -X POST "$BASE/api/escalations/T999/accept"
check 401 "POST /api/escalations/T999/accept   key 无效"                  -X POST -H "x-api-key: $BAD" "$BASE/api/escalations/T999/accept"
check 403 "POST /api/escalations/T999/accept   用 tenant key"             -X POST -H "x-api-key: $TK" "$BASE/api/escalations/T999/accept"
check 200 "POST /api/escalations/T999/accept   用 agent key（越门）"      -X POST -H "x-api-key: $AK" "$BASE/api/escalations/T999/accept"

check 401 "POST /api/escalations/T999/reply    无 key"                    -X POST -H 'Content-Type: application/json' -d '{"content":"hi"}' "$BASE/api/escalations/T999/reply"
check 403 "POST /api/escalations/T999/reply    用 tenant key"             -X POST -H "x-api-key: $TK" -H 'Content-Type: application/json' -d '{"content":"hi"}' "$BASE/api/escalations/T999/reply"
check 200 "POST /api/escalations/T999/reply    用 agent key（越门）"      -X POST -H "x-api-key: $AK" -H 'Content-Type: application/json' -d '{"content":"hi"}' "$BASE/api/escalations/T999/reply"

check 401 "POST /api/escalations/T999/close    无 key"                    -X POST "$BASE/api/escalations/T999/close"
check 403 "POST /api/escalations/T999/close    用 tenant key"             -X POST -H "x-api-key: $TK" "$BASE/api/escalations/T999/close"
check 200 "POST /api/escalations/T999/close    用 agent key（越门）"      -X POST -H "x-api-key: $AK" "$BASE/api/escalations/T999/close"

echo ""
echo "--- B. 知识库面板链路（get_current_tenant）---"
check 200 "GET  /api/knowledge                  无 key（兼容期读放行）"   "$BASE/api/knowledge"
check 401 "GET  /api/knowledge                  key 无效"                  -H "x-api-key: $BAD" "$BASE/api/knowledge"
check 200 "GET  /api/knowledge                  用 agent key"              -H "x-api-key: $AK" "$BASE/api/knowledge"

check 403 "POST /api/knowledge                  无 key（写拒绝）"          -X POST -H 'Content-Type: application/json' -d '{"product_id":"ZZ_H1_VERIFY","name":"H1鉴权验证临时条目"}' "$BASE/api/knowledge"
check 401 "POST /api/knowledge                  key 无效"                  -X POST -H "x-api-key: $BAD" -H 'Content-Type: application/json' -d '{"product_id":"ZZ_H1_VERIFY","name":"x"}' "$BASE/api/knowledge"

echo ""
echo "--- C. 知识库写入闭环（用 tenant key 建 → 改 → 删）---"
created=$(curl -s --noproxy '*' -m "$TIMEOUT" -X POST -H "x-api-key: $TK" -H 'Content-Type: application/json' \
  -d '{"product_id":"ZZ_H1_VERIFY","name":"H1鉴权验证临时条目","category":"测试","description":"仅用于鉴权闭环验证"}' \
  "$BASE/api/knowledge")
kid=$(printf '%s' "$created" | sed -n 's/.*"id":[ ]*\([0-9]\+\).*/\1/p')
if [ -n "$kid" ]; then
  echo "  OK   POST /api/knowledge                 用 tenant key 创建成功 id=$kid"
  pass=$((pass+1))
  check 403 "PUT    /api/knowledge/$kid                  无 key（写拒绝）" -X PUT -H 'Content-Type: application/json' -d '{"name":"x"}' "$BASE/api/knowledge/$kid"
  check 200 "PUT    /api/knowledge/$kid                  用 tenant key"    -X PUT -H "x-api-key: $TK" -H 'Content-Type: application/json' -d '{"name":"H1鉴权验证临时条目-已改"}' "$BASE/api/knowledge/$kid"
  check 403 "DELETE /api/knowledge/$kid                  无 key（写拒绝）" -X DELETE "$BASE/api/knowledge/$kid"
  check 200 "DELETE /api/knowledge/$kid                  用 tenant key"    -X DELETE -H "x-api-key: $TK" "$BASE/api/knowledge/$kid"
else
  echo "  FAIL POST /api/knowledge                 用 tenant key 创建失败: $(printf '%s' "$created" | head -c 200)"
  fail=$((fail+1))
fi

echo ""
echo "--- D. reindex 接口（H5；全量重建约 20-30s，故超时给足）---"
check 403 "POST /api/knowledge/reindex         无 key"                      -X POST "$BASE/api/knowledge/reindex"
check 401 "POST /api/knowledge/reindex         key 无效"                    -X POST -H "x-api-key: $BAD" "$BASE/api/knowledge/reindex"
check 200 "POST /api/knowledge/reindex         用 tenant key（真实重建）"   -X POST -H "x-api-key: $TK" "$BASE/api/knowledge/reindex"
echo "       reindex 返回: $(head -c 200 "$BODY" 2>/dev/null)"

echo ""
echo "=================================================="
echo "通过 $pass 项，失败 $fail 项"
echo "=================================================="
rm -f "$BODY"
[ "$fail" -eq 0 ]

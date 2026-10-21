#!/bin/bash
# 验证注册码消耗审计：查全部码的使用情况
set -u
B=http://127.0.0.1:8767
DB=/tmp/rbtest/e2e_admin.db

# 管理员登录口令只从环境读，不在仓库里落字面量（公开仓库等于已泄露）。
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
if [ -z "$ADMIN_PASSWORD" ]; then
  echo "缺少 ADMIN_PASSWORD（读自环境变量）。" >&2
  echo "例：ADMIN_PASSWORD=... bash scripts/e2e_code_audit.sh" >&2
  exit 2
fi

# 服务若未运行则启动（复用现有数据库）
if ! curl -s -m 2 -o /dev/null $B/; then
  DATABASE_URL="sqlite:///$DB" nohup python3 -m uvicorn backend.main:app --host 127.0.0.1 --port 8767 > /tmp/rbtest/admin_e2e.log 2>&1 &
  sleep 4
fi

A=$(curl -s -m 10 -X POST $B/api/admin/auth/login -H "Content-Type: application/json" -d '{"username":"boss","password":"'"$ADMIN_PASSWORD"'"}')
TOKEN=$(echo "$A" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -s -m 10 "$B/api/admin/registration-codes/list" -H "Authorization: Bearer $TOKEN" | python3 -c '
import sys, json
for c in json.load(sys.stdin)["codes"]:
    users = [u["username"] for u in c["used_by"]]
    print("code=%s use=%s/%s active=%s used_by=%s" % (c["code"], c["use_count"], c["max_uses"], c["is_active"], users))
'

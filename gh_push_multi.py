#!/usr/bin/env python3
"""经 custom.github 连接器用 Git Data API 推送分支（多文件），并开 PR。

用法: gh_push_multi.py
推送 worktree 当前 HEAD 的改动到 origin 的同名分支，然后开 PR 到 main。
从不打印凭证原文，只传 hsurr surrogate。
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import urllib.request
import urllib.error

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import add_surrogate_to_request, read_json_response  # noqa: E402

API = "https://api.github.com"
CRED = "custom.github"
ALLOWED = ["api.github.com"]
REPO = "xzb177/Aetrix-Portal"
BRANCH = "admin-polish/phase2-a1-bulk-select"


def api(method: str, path: str, payload: dict | None = None) -> dict:
    url = API + path
    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "muse-github-skill/1.0",
    }
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    add_surrogate_to_request(req, CRED, allowed_hosts=ALLOWED)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status == 204:
                return {}
            return read_json_response(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}")


def sh(*args: str) -> str:
    return subprocess.run(
        args, cwd="/home/hatch/workspace/worktrees/admin-a1-bulk",
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def main() -> None:
    files = sh("git", "diff", "--name-only", "origin/main...HEAD").splitlines()
    files += [l for l in sh("git", "ls-files", "--others", "--exclude-standard").splitlines()]
    files = sorted(set(files))
    print("files:", files)
    assert files, "no changes to push"

    # base: origin/main 的 commit 与 tree
    main_ref = api("GET", f"/repos/{REPO}/git/ref/heads/main")
    base_sha = main_ref["object"]["sha"]
    base_commit = api("GET", f"/repos/{REPO}/git/commits/{base_sha}")
    base_tree = base_commit["tree"]["sha"]
    print("base:", base_sha[:8])

    # 建 blobs
    tree_entries = []
    for path in files:
        with open(f"/home/hatch/workspace/worktrees/admin-a1-bulk/{path}", "rb") as f:
            content = f.read()
        blob = api("POST", f"/repos/{REPO}/git/blobs", {
            "content": base64.b64encode(content).decode(),
            "encoding": "base64",
        })
        tree_entries.append({
            "path": path, "mode": "100644", "type": "blob", "sha": blob["sha"],
        })

    # 建 tree / commit
    msg = sh("git", "log", "-1", "--format=%B", "HEAD")
    tree = api("POST", f"/repos/{REPO}/git/trees",
               {"base_tree": base_tree, "tree": tree_entries})
    commit = api("POST", f"/repos/{REPO}/git/commits",
                 {"message": msg, "tree": tree["sha"], "parents": [base_sha]})
    print("commit:", commit["sha"][:8])

    # 建（或更新）分支 ref
    try:
        api("POST", f"/repos/{REPO}/git/refs",
            {"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})
        print("branch created")
    except RuntimeError as e:
        if "422" in str(e):
            api("PATCH", f"/repos/{REPO}/git/refs/heads/{BRANCH}",
                {"sha": commit["sha"], "force": False})
            print("branch updated")
        else:
            raise

    # 开 PR（已存在则复用）
    body = (
        "## 改了什么\n"
        "- 新增 `composables/useTableSelection.ts`：极简选择状态（selectedKeys/selectedCount/clear）\n"
        "- 新增 `utils/runBulkOperation.ts`：有界并发（默认 2）批量执行器 + 中文汇总文案，失败不重试\n"
        "- `DataTable`：`selectable` / `v-model:selectedKeys` / `isRowSelectable` / `#bulk-bar` 插槽；"
        "桌面手写 checkbox 列（三态表头），手机卡片行首 checkbox；工具条桌面 absolute 覆盖、手机 fixed 底部；"
        "`selectable=false` 时零行为变化\n"
        "- `Users.vue`：批量禁用/启用（排除自己和超管），筛选提交清选择\n"
        "- `Orders.vue`：批量标记已支付（仅 pending）/ 批量退款对话框（逐条执行），现有单条逻辑不动\n"
        "- `Invitations.vue`：邀请码表批量作废（复用 `revokeInvitationCodes` 后端批量接口），筛选变化清选择\n"
        "- `ExchangeCodes.vue`：批量停用（前端筛选，本地更新状态免重拉）\n"
        "\n## 防负优化\n"
        "- 选择是纯前端状态，零新增请求；批量执行并发=2\n"
        "- 工具条 overlay/fixed 定位，不引起布局跳动；`prefers-reduced-motion` 下关闭位移动画\n"
        "- 仅 4 个 view 开启 selectable，其余 30+ 个 view 的 DataTable 行为零变化\n"
        "- `npm run type-check` + `npm run build` 均通过\n"
    )
    try:
        pr = api("POST", f"/repos/{REPO}/pulls", {
            "title": "[admin-polish-p2-a1] DataTable 行选择与批量工具条",
            "head": BRANCH,
            "base": "main",
            "body": body,
        })
        print("PR:", pr["html_url"])
    except RuntimeError as e:
        if "422" in str(e):
            prs = api("GET", f"/repos/{REPO}/pulls?head=xzb177:{BRANCH}&state=open")
            print("PR exists:", prs[0]["html_url"] if prs else "unknown")
        else:
            raise


if __name__ == "__main__":
    main()

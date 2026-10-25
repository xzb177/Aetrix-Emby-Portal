"""L50 回归守卫：部署脚本不许出现未认证的 api.github.com 调用。

背景：匿名 GitHub API 限额 60/h，2026-10-20（#1150）与 2026-10-25 两度耗尽，
致 auto-merge-deploy 取 main tip 失败、部署 ABORT。2026-10-25 已改走认证通道
（gh_main_tip.py 与 pr_check_and_merge.py 的 custom.github 认证）；本测试保证
没人把匿名调用写回来。

断言范围（取 tip 与合并/部署路径为准）：
- 仓库面（CI 恒跑）：scripts/ 下 .sh/.py、.github/ 下 .yml/.yaml/.sh、仓库根 *.sh。
- 运维面（运维机上跑；CI 干净检出无此目录时跳过并明说）：~/workspace/bin 下的
  auto-merge-deploy.sh、deploy-backend.sh、deploy-frontend.sh、gh_main_tip.py，
  以及 ~/workspace/skills/github/bin/pr_check_and_merge.py。

判定规则（不是 blanket 断言）：
- .sh/.yml/.yaml：含 api.github.com 的逻辑行（先拼续行、再去整行注释）是违规，
  除非同行含 Authorization 或 GITHUB_TOKEN（即认证调用）。
- .py：文件含 api.github.com 且无认证标记（add_surrogate_to_request 或
  Authorization）才是违规；带认证标记的文件视为走认证通道。
- 本来就不含 api.github.com 的合法调用（公开仓库匿名 git fetch、健康检查 curl
  等）自然不在断言里；若将来出现必须匿名的 api.github.com 调用，须进下面的
  ALLOWLIST 并写清理由，否则测试直接失败。
- 已知局限（写在明处）：.py 按文件级认证标记判定——已认证文件内若有人绕开
  统一封装裸调 api.github.com，本测试看不出来；两条已知认证路径（gh_main_tip.py
  的单请求、pr_check_and_merge.py 的 api_request 统一封装）均把全部请求走认证，
  改封装时须同步复核。
"""

from __future__ import annotations

import os
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
API_HOST = "api.github.com"

# 显式 allowlist：路径（相对仓库根，或运维面绝对路径）-> 放行理由。
# 目前为空：没有任何匿名的 api.github.com 调用需要放行。新增条目时必须写清
# “为什么必须匿名”，且 test_allowlist_entries_still_apply 会盯着它不过期。
ALLOWLIST: dict[str, str] = {}

# 运维面部署路径文件（取 tip + 合并 + 部署三段全覆盖）。
OPS_BIN_FILES = (
    "auto-merge-deploy.sh",
    "deploy-backend.sh",
    "deploy-frontend.sh",
    "gh_main_tip.py",
)
OPS_SKILLS_FILE = (
    pathlib.Path.home()
    / "workspace"
    / "skills"
    / "github"
    / "bin"
    / "pr_check_and_merge.py"
)


def _logical_lines(text: str):
    """拼续行：返回 [(首行行号, 逻辑行)]，行号从 1 计。"""
    out = []
    buf = ""
    start = 0
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\n")
        if buf == "":
            start = i
            buf = line
        else:
            buf += line
        if buf.endswith("\\"):
            buf = buf[:-1]
            continue
        out.append((start, buf))
        buf = ""
    if buf:
        out.append((start, buf))
    return out


def find_anonymous_calls(text: str, suffix: str):
    """返回未认证 api.github.com 调用的 [(行号, 行内容)]，无则 []。"""
    if suffix == ".py":
        if API_HOST not in text:
            return []
        if "add_surrogate_to_request" in text or "Authorization" in text:
            return []
        return [
            (i, ln.strip())
            for i, ln in enumerate(text.splitlines(), 1)
            if API_HOST in ln and not ln.strip().startswith("#")
        ]
    if suffix in (".sh", ".yml", ".yaml"):
        out = []
        for lineno, ln in _logical_lines(text):
            if ln.strip().startswith("#"):
                continue
            if (
                API_HOST in ln
                and "Authorization" not in ln
                and "GITHUB_TOKEN" not in ln
            ):
                out.append((lineno, ln.strip()))
        return out
    return []


def _scan_file(path: pathlib.Path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return find_anonymous_calls(text, path.suffix)


def _format(violations: dict) -> str:
    lines = []
    for name in sorted(violations):
        for lineno, line in violations[name]:
            lines.append(f"{name}:{lineno}: {line}")
    return "\n".join(lines)


# --- 扫描器自身的行为测试（合成用例，CI 恒跑） ---


def test_scanner_flags_bare_curl():
    text = (
        "MAIN_TIP=$(curl -s -m 20 -H \"Accept: application/vnd.github+json\" \\\n"
        "  \"https://api.github.com/repos/xzb177/Aetrix-Emby-Portal/commits/main\" "
        "| jq -r '.sha // empty')\n"
    )
    found = find_anonymous_calls(text, ".sh")
    assert len(found) == 1
    assert found[0][0] == 1


def test_scanner_allows_authorized_curl():
    text = (
        "curl -s -H \"Authorization: Bearer $TOKEN\" "
        "https://api.github.com/repos/xzb177/Aetrix-Emby-Portal/commits/main\n"
    )
    assert find_anonymous_calls(text, ".sh") == []


def test_scanner_allows_github_token_curl():
    text = (
        "curl -s -H \"Authorization: token ${{ secrets.GITHUB_TOKEN }}\" "
        "https://api.github.com/repos/xzb177/Aetrix-Emby-Portal/pulls\n"
    )
    assert find_anonymous_calls(text, ".yml") == []


def test_scanner_allows_authenticated_python():
    text = (
        "import urllib.request\n"
        "from dynamic_credentials import add_surrogate_to_request\n"
        "req = urllib.request.Request(\"https://api.github.com/repos/x/y\")\n"
        "add_surrogate_to_request(req, \"custom.github\", "
        "allowed_hosts=[\"api.github.com\"])\n"
    )
    assert find_anonymous_calls(text, ".py") == []


def test_scanner_flags_unauthenticated_python():
    text = (
        "import urllib.request, json\n"
        "with urllib.request.urlopen("
        "\"https://api.github.com/repos/x/y\") as r:\n"
        "    data = json.load(r)\n"
    )
    found = find_anonymous_calls(text, ".py")
    assert len(found) == 1


def test_scanner_ignores_public_fetch_and_health_checks():
    text = (
        "# 公开仓库匿名只读抓取：不走 api.github.com，本就不在断言里\n"
        "git fetch https://github.com/xzb177/Aetrix-Emby-Portal.git main\n"
        "curl -s -m 15 http://127.0.0.1:8000/api/health\n"
        "# 旧的匿名调用只留在注释里也不算调用\n"
        "# curl -s https://api.github.com/repos/x/y\n"
    )
    assert find_anonymous_calls(text, ".sh") == []


# --- 仓库面：CI 恒跑 ---


def _repo_deploy_files():
    files = []
    for root in (REPO / "scripts", REPO / ".github"):
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if p.is_file() and p.suffix in (".sh", ".py", ".yml", ".yaml"):
                    files.append(p)
    files.extend(sorted(REPO.glob("*.sh")))
    return files


def test_repo_deploy_surface_has_no_anonymous_calls():
    violations = {}
    for path in _repo_deploy_files():
        rel = str(path.relative_to(REPO))
        if rel in ALLOWLIST:
            continue
        found = _scan_file(path)
        if found:
            violations[rel] = found
    assert not violations, (
        "仓库部署面发现未认证 api.github.com 调用（改走认证通道，或进 ALLOWLIST "
        "并写清必须匿名的理由）：\n" + _format(violations)
    )


def test_allowlist_entries_still_apply():
    for rel in ALLOWLIST:
        path = REPO / rel
        assert path.is_file(), f"ALLOWLIST 已过期（文件不存在）：{rel}，请删除该条目"
        assert API_HOST in path.read_text(
            encoding="utf-8", errors="replace"
        ), f"ALLOWLIST 已过期（文件已无 api.github.com 引用）：{rel}，请删除该条目"


# --- 运维面：运维机上跑，CI 干净检出时跳过 ---


def _ops_bin_dir() -> pathlib.Path:
    override = os.environ.get("AETRIX_OPS_SCRIPTS_DIR")
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / "workspace" / "bin"


def test_ops_deploy_path_has_no_anonymous_calls():
    bin_dir = _ops_bin_dir()
    if not bin_dir.is_dir():
        pytest.skip(
            f"运维脚本目录不存在（CI 干净检出）：{bin_dir}；仓库面断言仍在跑，"
            "运维面请在运维机上跑本文件复核"
        )
    missing = [n for n in OPS_BIN_FILES if not (bin_dir / n).is_file()]
    assert not missing, (
        "运维部署脚本缺失（若有意删除请同步更新 OPS_BIN_FILES）："
        + ", ".join(missing)
    )
    violations = {}
    for name in OPS_BIN_FILES:
        found = _scan_file(bin_dir / name)
        if found:
            violations[name] = found
    if OPS_SKILLS_FILE.is_file():
        found = _scan_file(OPS_SKILLS_FILE)
        if found:
            violations[str(OPS_SKILLS_FILE)] = found
    assert not violations, (
        "运维部署路径发现未认证 api.github.com 调用（改走认证通道）：\n"
        + _format(violations)
    )

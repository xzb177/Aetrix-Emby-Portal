"""播放地址口径的单元测试（v2.23.1）

两套口径必须分清楚，混在一起就会「要么放坏部署、要么挡不住 SSRF」：

- **配置来源的直链**（媒体库路径 / 存储挂载 / `.strm` 内容）：由有管理权限的运维配置，
  指向内网是**正常用法**（局域网 NAS、自建 WebDAV / AList / MinIO、本机回环转发器）。
  默认放行，只拦真正无歧义的：非 http(s)、URL 里带内嵌凭据。
  需要更严的部署可以打开 `EMBY_BLOCK_PRIVATE_MEDIA_URLS`，并用
  `EMBY_MEDIA_URL_ALLOWLIST` 把自己的 NAS / 回环地址放行。
- **服务器自己追出去的重定向目标**：源站回一个 302 指向内网运维接口 / 云元数据时，
  代理会带着你的 Cookie / Basic 去取——这是真正能被第三方利用的 SSRF，**一律拦**，
  且不受上面那个开关影响（同时跨主机重定向会剥掉凭据头）。

另外钉住一条容易写错的细节：**域名解析不了不等于它指向内网**。曾经的实现在这儿
fail-closed，于是离线 / 内网 DNS / 临时解析故障会把公开 CDN 直链也一并拒掉。

（2026-10-17 转码/中继删除：服务端不再追重定向取字节，
``streaming._next_redirect`` / ``_redirect_headers`` 已随挂载中继删除；
下面的重定向目标严格口径仍由 ``playback_security.reject_private_url``
保留，供配置面校验用。）
"""
import pytest

from backend.emby_server import playback_security as pb
from backend.emby_server.mounts import MountError

# 用 IP 字面量写用例：不依赖测试机器的 DNS，结果才稳定
PUBLIC = "https://93.184.216.34/movie.mkv"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例都从「默认口径」开始（不受运行环境里的少量变量影响）"""
    monkeypatch.delenv("EMBY_BLOCK_PRIVATE_MEDIA_URLS", raising=False)
    monkeypatch.delenv("EMBY_MEDIA_URL_ALLOWLIST", raising=False)


# ==================== 配置来源：默认口径 ====================

def test_public_url_allowed():
    assert pb.validate_remote_url(PUBLIC) == PUBLIC


def test_private_url_allowed_by_default():
    assert pb.validate_remote_url("http://192.168.1.10:8096/movie.mkv")


def test_loopback_url_allowed_by_default():
    assert pb.validate_remote_url("http://127.0.0.1:5244/dav/movie.mkv")


def test_unresolvable_host_is_not_internal():
    """解析不了 ≠ 内网：真正的请求会以「源站不可达」如实失败"""
    assert pb.validate_remote_url("https://no-such-host.invalid/x.mkv")


def test_non_http_scheme_rejected():
    with pytest.raises(MountError):
        pb.validate_remote_url("file:///etc/passwd")


def test_embedded_credentials_rejected():
    with pytest.raises(MountError):
        pb.validate_remote_url("http://user:pass@93.184.216.34/x.mkv")


# ==================== 配置来源：打开严格模式 ====================

def test_strict_mode_rejects_private(monkeypatch):
    monkeypatch.setenv("EMBY_BLOCK_PRIVATE_MEDIA_URLS", "1")
    with pytest.raises(MountError):
        pb.validate_remote_url("http://10.0.0.5/movie.mkv")


def test_strict_mode_allowlist_carve_out(monkeypatch):
    monkeypatch.setenv("EMBY_BLOCK_PRIVATE_MEDIA_URLS", "1")
    monkeypatch.setenv("EMBY_MEDIA_URL_ALLOWLIST", "10.0.0.5")
    assert pb.validate_remote_url("http://10.0.0.5/movie.mkv")


def test_strict_mode_keeps_public_urls(monkeypatch):
    monkeypatch.setenv("EMBY_BLOCK_PRIVATE_MEDIA_URLS", "1")
    assert pb.validate_remote_url(PUBLIC) == PUBLIC


# ==================== 重定向目标：一律严格 ====================

@pytest.mark.parametrize("target", [
    "http://10.0.0.5/secret",                 # 内网
    "http://169.254.169.254/latest/meta-data/",  # 链路本地（云元数据）
    "http://localhost:8096/System/Info",      # 回环主机名
    "http://[::1]/secret",                    # IPv6 回环
])
def test_redirect_target_rejects_internal(target):
    with pytest.raises(MountError):
        pb.reject_private_url(target)


def test_redirect_target_keeps_public():
    assert pb.reject_private_url("https://93.184.216.34/other.mkv")


def test_redirect_targets_strict_regardless_of_switch(monkeypatch):
    """严格开关只管「配置来源」：重定向目标无论开关如何都拦内网"""
    monkeypatch.setenv("EMBY_BLOCK_PRIVATE_MEDIA_URLS", "0")
    with pytest.raises(MountError):
        pb.reject_private_url("http://10.0.0.5/secret")


def test_allowlist_is_an_explicit_carve_out_everywhere(monkeypatch):
    """allowlist 是运维的**显式**决定：配置来源与重定向目标都认它

    （不是「随手放行」：写成这个名字的地址等于「这个主机我知道是谁」。）
    """
    monkeypatch.setenv("EMBY_MEDIA_URL_ALLOWLIST", "10.0.0.5")
    assert pb.reject_private_url("http://10.0.0.5/secret")
    monkeypatch.setenv("EMBY_BLOCK_PRIVATE_MEDIA_URLS", "1")
    assert pb.validate_remote_url("http://10.0.0.5/secret")

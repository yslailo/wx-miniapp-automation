# -*- coding: utf-8 -*-
"""
复用同一套改进版 freeproxy fork：
  · ip2region 本地离线定位（替代逐个 IP 调外部地理 API，秒级完成）
  · 边抓边验、找到可用即停（fetch_working_streaming，凑够 need 个立刻返回）
  · 只筛中国大陆 http/https 代理

探针 = 「提现笔笔省」自己的查询接口。判定标准：能从该代理拿到带 "errcode" 字段的
JSON 业务响应 → 证明这个代理能连上 discount.wxpapp.wechatpay.cn 且绕过了 WAF。
注意：token 失效时服务端仍会返回带 errcode 的 JSON（如 268566816），因此用这个接口
做连通性验证不受 token 寿命影响。

接入方式（main.py 已自动注册，无需手动调用）：
    import net, proxy_pool
    net.set_proxy_provider(proxy_pool.fetch_working_proxies)
"""

import json
import zlib
import logging

logger = logging.getLogger(__name__)

# 只用免浏览器抓取的国内源（与 tastin-sign 完全一致）；
# 已剔除实测无效的 GoodIPS。
PROXY_SOURCES = [
    "KuaidailiProxiedSession", "QiyunipProxiedSession", "KxdailiProxiedSession",
    "IP89ProxiedSession", "TheSpeedXProxiedSession", "ProxyScrapeProxiedSession",
]

# 缓存当前批次的可用代理；某代理失效时由调用方 clear 后重新抓取
_working = []


def _get_headers() -> dict:
    """复用业务请求头（含 session-token）作为探针请求头。
    token 无效也能验证代理连通性（服务端仍返回 JSON errcode）。"""
    try:
        from main import build_headers  # 延迟导入，避免循环
        return build_headers()
    except Exception:  # noqa: BLE001
        # 兜底：最小可用请求头（够触发 JSON 响应即可）
        return {
            "X-Page": "pages/gift/index",
            "xweb_xhr": "1",
            "X-Module-Name": "mmpaytxbbsmp",
            "X-Appid": "wxdb3c0e388702f785",
            "Content-Type": "application/json",
            "Referer": "https://servicewechat.com/wxdb3c0e388702f785/185/page-frame.html",
        }


def _get_query_url() -> str:
    try:
        from main import QUERY_URL  # 延迟导入
        return QUERY_URL
    except Exception:  # noqa: BLE001
        return "https://discount.wxpapp.wechatpay.cn/txbbs-mall/coupon/querydailygiftcoupons"


def _is_valid(resp) -> bool:
    """能解析出带 errcode 的 JSON → 该代理可用且绕过 WAF。
    兼容服务端的双层压缩（外层 zlib + 内层 raw deflate）。"""
    try:
        data = resp.json()
        if isinstance(data, dict) and "errcode" in data:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        data = json.loads(zlib.decompress(resp.content, -15).decode("utf-8"))
        return isinstance(data, dict) and "errcode" in data
    except Exception:  # noqa: BLE001
        return False


def clear():
    """清空缓存（代理全部失效时调用，触发下一次重新抓取）。"""
    global _working
    _working = []


def fetch_working_proxies(need: int = 3) -> list:
    """获取并验证国内免费代理，返回 requests 格式的代理 dict 列表。
    已有缓存则直接返回；结果为空时返回 []（调用方自行决定是否直连）。"""
    global _working
    if _working:
        return _working

    try:
        from freeproxy.freeproxy import ProxiedSessionClient
    except ImportError:
        logger.warning("未安装代理库（freeproxy fork），跳过代理池；如需代理请 pip install -r requirements.txt")
        return []

    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:  # noqa: BLE001
        pass

    logger.info("并发抓取+验证国内免费代理（边抓边验，找到可用即停）...")
    client = ProxiedSessionClient(
        proxy_sources=PROXY_SOURCES,
        init_proxied_session_cfg={
            "max_pages": 2,
            "filter_rule": {"country_code": ["CN"], "protocol": ["http", "https"]},
        },
        disable_print=True,
        lazy=True,  # 不在构造阶段抓取，交给 fetch_working_streaming 命中即停
    )
    _working = client.fetch_working_streaming(
        test_url=_get_query_url(),
        headers=_get_headers(),
        need=need,
        source_timeout=15,
        validate_timeout=10,
        validate_workers=64,
        method="GET",
        is_valid=_is_valid,
    ) or []
    logger.info("可用代理 %d 个", len(_working))
    return _working

# -*- coding: utf-8 -*-
"""
统一请求层 —— 直连优先，代理池可插拔（后续接入，不在本阶段实现）。

设计目标：
  · 现在：直连即可跑通「提现笔笔省」领券（GitHub Actions 海外 IP 实测未被墙时无需代理）。
  · 以后：如果发现 discount.wxpapp.wechatpay.cn 对海外 IP / 数据中心 IP 做了 WAF 或风控拦截，
          直接把塔斯汀那套「国内免费代理池」注入进来即可，业务代码零改动。

注入方式（任选其一，详见 resolve_proxy_pool()）：
  1. 环境变量 PROXY_LIST="http://ip:port,http://ip2:port2"        ← 手动/静态
  2. 仓库根目录 proxies.json = [{"http":"http://ip:port","https":"http://ip:port"}, ...]
  3. 代码里调用 set_proxy_provider(fn)，fn() 返回上述 dict 列表  ← 接塔斯汀 freeproxy 池走这里

自动回退逻辑：
  直连返回「疑似 WAF」响应（403/405、HTML 而非 JSON）时，若代理池里有可用代理，
  自动改用代理重试一次；否则原样返回，由上层处理。
"""

import os
import json
import logging
import requests

logger = logging.getLogger(__name__)

TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "15"))

# 代理池提供者：一个返回 [{"http":..., "https":...}, ...] 的可调用对象
_proxy_provider = None
# 代理池缓存
_proxy_pool = []


def set_proxy_provider(fn) -> None:
    """注入代理池提供者。示例：
        from myproxy import fetch_working_proxies
        net.set_proxy_provider(fetch_working_proxies)
    fn() 应为无参函数，返回 requests 格式的代理 dict 列表。
    """
    global _proxy_provider, _proxy_pool
    _proxy_provider = fn
    _proxy_pool = []


def resolve_proxy_pool() -> list:
    """解析出当前可用代理列表（带缓存）。后续接塔斯汀 freeproxy 池只需 set_proxy_provider。"""
    global _proxy_pool
    if _proxy_pool:
        return _proxy_pool

    # 1) 代码注入的提供者优先
    if _proxy_provider is not None:
        try:
            _proxy_pool = list(_proxy_provider() or [])
        except Exception as e:  # noqa: BLE001
            logger.warning("代理提供者调用失败: %s", e)
            _proxy_pool = []
        if _proxy_pool:
            return _proxy_pool

    # 2) 静态环境变量 PROXY_LIST
    raw = os.environ.get("PROXY_LIST", "").strip()
    if raw:
        _proxy_pool = [{"http": p.strip(), "https": p.strip()}
                       for p in raw.split(",") if p.strip()]
        return _proxy_pool

    # 3) 本地 proxies.json
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies.json")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                _proxy_pool = [d for d in data if isinstance(d, dict)]
        except Exception as e:  # noqa: BLE001
            logger.warning("读取 proxies.json 失败: %s", e)

    return _proxy_pool


def _looks_like_waf(resp: requests.Response) -> bool:
    """疑似 WAF/风控拦截：非 JSON 的 HTML 页面，或 403/405。"""
    if resp.status_code in (403, 405):
        return True
    ctype = resp.headers.get("Content-Type", "")
    body = resp.content[:200].lower()
    if "text/html" in ctype or b"<html" in body or b"<!doctype" in body:
        return True
    return False


def request(method: str, url: str, headers: dict, json_body=None) -> requests.Response:
    """统一请求入口。直连优先；疑似被 WAF 拦时惰性拉取代理池并重试。

    注意：代理池是「按需」获取的 —— 直连正常时绝不会去抓代理，
    避免每次运行都白抓一轮免费代理（这也是模仿 tastin-sign 的直连优先策略）。
    """
    use_proxy = os.environ.get("USE_PROXY", "").lower() in ("1", "true", "yes")

    # 强制代理模式：直接拉池
    if use_proxy:
        pool = resolve_proxy_pool()
        if pool:
            return _via_proxy(method, url, headers, json_body, pool)
        logger.warning("USE_PROXY=1 但代理池为空，回退直连")

    # ---- 直连 ----
    try:
        resp = _send(method, url, headers, json_body, proxies=None)
    except requests.RequestException as e:
        # 直连网络层就失败 → 才去拉代理池兜底
        pool = resolve_proxy_pool()
        if pool:
            logger.warning("直连失败(%s)，改用代理重试", e)
            return _via_proxy(method, url, headers, json_body, pool)
        raise

    # ---- 直连被 WAF 拦 → 惰性拉池回退 ----
    if _looks_like_waf(resp):
        pool = resolve_proxy_pool()
        if pool:
            logger.warning("直连疑似被 WAF 拦截(HTTP %s)，改用代理重试", resp.status_code)
            try:
                proxied = _via_proxy(method, url, headers, json_body, pool)
                if not _looks_like_waf(proxied):
                    return proxied
            except requests.RequestException as e:
                logger.warning("代理重试失败: %s", e)

    return resp


def _via_proxy(method, url, headers, json_body, pool) -> requests.Response:
    """依次尝试代理池中的代理，返回第一个不疑似被拦的响应。"""
    last_resp = None
    last_err = None
    for proxies in list(pool):
        try:
            resp = _send(method, url, headers, json_body, proxies=proxies)
        except requests.RequestException as e:
            last_err = e
            continue
        if not _looks_like_waf(resp):
            return resp
        last_resp = resp
    if last_resp is not None:
        return last_resp
    if last_err is not None:
        raise last_err
    raise requests.RequestException("代理池为空")


def _send(method, url, headers, json_body, proxies) -> requests.Response:
    method = method.upper()
    kwargs = dict(headers=headers, timeout=TIMEOUT, proxies=proxies, verify=False)
    if method == "GET":
        return requests.get(url, **kwargs)
    if method == "POST":
        return requests.post(url, json=json_body, **kwargs)
    return requests.request(method, url, json=json_body, **kwargs)

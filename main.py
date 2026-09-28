# -*- coding: utf-8 -*-
"""
微信支付「提现笔笔省」每日领券 —— 主脚本

退出码（供 GitHub Actions 判定，也用于"token 能活多久"实验）：
  0  成功（含"今日已领取"）
  1  业务失败 / 网络失败
  2  SESSION_TOKEN 已失效（需重新抓取并更新 Secret）—— 鉴权错误 errcode 268566816

日志约定：
  每次运行都会打印 [probe] token_fp=<8位指纹> len=<长度> utc=<时间>
  指纹 = sha256(token) 前 8 位，不含明文；对比两天日志即可确认"是否同一个 token 在跑"。
"""

import os
import sys
import time
import zlib
import json
import hashlib
import secrets
import logging
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

import net

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

BASE = "https://discount.wxpapp.wechatpay.cn"
QUERY_URL = f"{BASE}/txbbs-mall/coupon/querydailygiftcoupons"
CLAIM_URL = f"{BASE}/txbbs-mall/coupon/claimdailygiftcoupon"

# 退出码
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_TOKEN_EXPIRED = 2

# 鉴权失效错误码（见 README）
AUTH_EXPIRED_ERRCODES = {268566816}
# 通用「页面已过期」错误码：今日已领 or 鉴权问题的模糊错误
SOFT_ERRCODES = {268592143}

# 注册国内代理池（模仿 tastin-sign）。仅当直连失败或疑似被 WAF 拦时才会真正去抓代理，
# 直连正常时零开销。freeproxy 未安装时 fetch_working_proxies() 返回空列表，自动降级为直连。
try:
    import proxy_pool
    net.set_proxy_provider(proxy_pool.fetch_working_proxies)
except Exception as _e:  # noqa: BLE001
    logger.warning("代理池注册失败（将仅使用直连）: %s", _e)


def token_fingerprint(tok: str) -> str:
    """token 指纹：sha256 前 8 位，可安全写入日志（不可反推）。"""
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()[:8]


def build_headers() -> dict:
    ms = int(time.time() * 1000)
    return {
        "X-Page": "pages/gift/index",
        "X-Track-Id": f"TA{secrets.token_hex(9).upper()}{ms}",
        "xweb_xhr": "1",
        "session-token": os.environ["SESSION_TOKEN"],
        "X-Module-Name": "mmpaytxbbsmp",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/132.0.0.0 Safari/537.36 "
            "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
            "MiniProgramEnv/Mac MacWechat/WMPF "
            "MacWechat/3.8.7(0x13080712) "
            "UnifiedPCMacWechat(0xf2641702) XWEB/18788"
        ),
        "Content-Type": "application/json",
        "session-id": f"daily_reward-{ms}-{secrets.token_hex(5)}",
        "X-Appid": "wxdb3c0e388702f785",
        "Accept": "*/*",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": "https://servicewechat.com/wxdb3c0e388702f785/185/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


def parse_response(resp: requests.Response) -> dict:
    """先尝试直接 JSON 解析；失败则做一次 raw deflate 再解析（应对服务端双层压缩）。"""
    try:
        return resp.json()
    except (json.JSONDecodeError, requests.exceptions.JSONDecodeError):
        inner = zlib.decompress(resp.content, -15)
        return json.loads(inner.decode("utf-8"))


def request_json(method: str, url: str, body: dict | None = None) -> dict:
    resp = net.request(method, url, build_headers(), json_body=body)
    resp.raise_for_status()
    return parse_response(resp)


def main() -> int:
    token = os.environ.get("SESSION_TOKEN", "")
    if not token:
        logger.error("缺少 SESSION_TOKEN 环境变量")
        return EXIT_TOKEN_EXPIRED

    logger.info("[probe] token_fp=%s len=%d utc=%s",
                token_fingerprint(token), len(token),
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    # 1. 查询当日券信息
    logger.info("查询每日礼物券信息...")
    query = request_json("GET", QUERY_URL)
    errcode = query.get("errcode")
    if errcode != 0:
        if errcode in AUTH_EXPIRED_ERRCODES:
            logger.error("[probe] AUTH_EXPIRED errcode=%s msg=%s —— SESSION_TOKEN 已失效",
                         errcode, query.get("msg"))
            return EXIT_TOKEN_EXPIRED
        logger.error("查询失败: errcode=%s msg=%s", errcode, query.get("msg"))
        return EXIT_FAIL

    data = query["data"]
    items = data.get("coupon_items", [])
    if not items:
        logger.warning("当前没有可领取的券")
        return EXIT_OK

    item = items[0]
    info = item["coupon_info"]
    coupon_id = info["coupon_id"]
    face_value = info["face_value"]
    name = info.get("name", "")
    is_claimed = item.get("is_claimed", False)
    exposure_token = data.get("exposure_token", "")

    logger.info("券信息: %s coupon_id=%s face_value=%s", name, coupon_id, face_value)

    if is_claimed:
        logger.info("今日已领取，无需重复领取")
        return EXIT_OK

    # 2. 领取
    logger.info("开始领取...")
    result = request_json("POST", CLAIM_URL, {
        "coupon_id": coupon_id,
        "daily_gift_type": "DGCT_PLATFORM",
        "expected_send_amount": face_value,
        "exposure_token": exposure_token,
    })
    rc = result.get("errcode")
    if rc == 0:
        logger.info("领券成功: %s", result)
        return EXIT_OK
    if rc in AUTH_EXPIRED_ERRCODES:
        logger.error("[probe] AUTH_EXPIRED errcode=%s msg=%s —— SESSION_TOKEN 已失效",
                     rc, result.get("msg"))
        return EXIT_TOKEN_EXPIRED
    if rc in SOFT_ERRCODES:
        # 查询阶段已确认 token 有效；此处的"页面已过期"多为今日已领/重复领取
        logger.warning("领券返回通用错误 errcode=%s msg=%s（token 有效，视为已处理）",
                       rc, result.get("msg"))
        return EXIT_OK
    logger.warning("领券失败: errcode=%s msg=%s", rc, result.get("msg"))
    return EXIT_FAIL


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.HTTPError as e:
        logger.error("HTTP 错误: %s", e)
        sys.exit(EXIT_FAIL)
    except Exception as e:  # noqa: BLE001
        logger.error("未知错误: %s", e, exc_info=True)
        sys.exit(EXIT_FAIL)

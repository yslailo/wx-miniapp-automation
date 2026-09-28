# -*- coding: utf-8 -*-
"""
session-token 寿命 & 轮换探测 —— 用来回答"提现笔笔省能不能像塔斯汀一样纯云端跑"。

背景：
  塔斯汀能纯云，是因为它的 user-token 寿命几天~几周，塞进 Secret 后 cron 定时用很久都不失效。
  提现笔笔省的 session-token 只有约 30~60 分钟，Secret 里的值等 cron 跑到时早就废了，
  所以必须先量化三件事，才能决定是否存在"纯云端"路径：

    1. 真实 TTL       —— token 从首次可用到最后一次可用的时长
    2. 是否会被刷新   —— 每次请求的响应头/响应体里是否下发新的 session-token
    3. 海外 IP 是否被拒 —— 在 GitHub Actions(海外) 请求是否被 WAF/风控挡（本地跑则测第 3 点无效）

用法（本地，需先抓到一个新鲜 token）：
    $env:SESSION_TOKEN="xxx"; python probe_token_ttl.py

    可选：
    $env:INTERVAL="300"      # 探测间隔秒，默认 300
    $env:MAX_SECONDS="18000" # 最长探测时长，默认 18000(5h)
    $env:ROTATE_FILE="rotated_token.txt"  # 若响应里出现新 token，落盘到这里

在 GitHub Actions 里跑同款脚本，可一次同时验证第 1、2、3 点（见 .github/workflows/probe-token-ttl.yml）。
"""

import os
import time
import zlib
import json
import secrets
import logging
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("probe")

BASE = "https://discount.wxpapp.wechatpay.cn"
QUERY_URL = f"{BASE}/txbbs-mall/coupon/querydailygiftcoupons"

INTERVAL = int(os.environ.get("INTERVAL", "300"))
MAX_SECONDS = int(os.environ.get("MAX_SECONDS", "18000"))
ROTATE_FILE = os.environ.get("ROTATE_FILE", "rotated_token.txt")

# 从环境变量 / .env 取 token
TOKEN = os.environ.get("SESSION_TOKEN", "")
if not TOKEN:
    try:
        from dotenv import load_dotenv
        load_dotenv()
        TOKEN = os.environ.get("SESSION_TOKEN", "")
    except Exception:
        pass

# 判定 token 失效的错误码（见仓库 README）
ERR_NOT_LOGIN = 268566816
ERR_PAGE_EXPIRED = 268592143


def build_headers(token: str) -> dict:
    """与 main.py 完全一致的请求头，只是 token 固定为被测值。"""
    ms = int(time.time() * 1000)
    return {
        "X-Page": "pages/gift/index",
        "X-Track-Id": f"TA{secrets.token_hex(9).upper()}{ms}",
        "xweb_xhr": "1",
        "session-token": token,
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
        "Referer": "https://servicewechat.com/wxdb3c0e388702f785/185/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


def parse_response(resp: requests.Response) -> dict:
    """先 JSON；失败则 raw deflate 再解析（应对双层压缩）。"""
    try:
        return resp.json()
    except Exception:
        inner = zlib.decompress(resp.content, -15)
        return json.loads(inner.decode("utf-8"))


def find_new_token(resp: requests.Response) -> str | None:
    """从响应头里找服务端可能下发的新 token（轮换检测）。"""
    for k, v in resp.headers.items():
        lk = k.lower()
        if "session" in lk and "token" in lk and v and v != TOKEN:
            return v
    return None


def main():
    if not TOKEN:
        logger.error("缺少 SESSION_TOKEN，请先设置环境变量或 .env")
        return

    logger.info("=" * 60)
    logger.info("session-token 寿命探测启动")
    logger.info("token 前缀: %s...（共 %d 字符）", TOKEN[:8], len(TOKEN))
    logger.info("探测间隔: %ds，最长: %ds", INTERVAL, MAX_SECONDS)
    logger.info("=" * 60)

    start = time.monotonic()
    last_ok = 0.0
    first_ok = None
    round_no = 0

    while True:
        elapsed = time.monotonic() - start
        if elapsed > MAX_SECONDS:
            logger.info("达到最长探测时长，停止。")
            break

        round_no += 1
        try:
            resp = requests.get(QUERY_URL, headers=build_headers(TOKEN), timeout=15)
            try:
                data = parse_response(resp)
            except Exception:
                # 解不开就是被 WAF 拦成 HTML，或响应异常
                logger.warning("[%4.0fs] #%d HTTP %s 非 JSON（疑似 WAF/风控拦截）头: %s",
                               elapsed, round_no, resp.status_code,
                               resp.text[:120].replace("\n", " "))
                time.sleep(INTERVAL)
                continue

            errcode = data.get("errcode")
            rotated = find_new_token(resp)

            if errcode == 0:
                if first_ok is None:
                    first_ok = elapsed
                last_ok = elapsed
                logger.info("[%4.0fs] #%d ✅ errcode=0 可用%s",
                            elapsed, round_no,
                            "  ⚠️ 响应头出现新 session-token（可轮换！）" if rotated else "")
                if rotated:
                    with open(ROTATE_FILE, "w", encoding="utf-8") as f:
                        f.write(rotated)
                    logger.info("      → 新 token 已写入 %s", ROTATE_FILE)
            elif errcode in (ERR_NOT_LOGIN, ERR_PAGE_EXPIRED):
                logger.info("[%4.0fs] #%d ❌ errcode=%s msg=%s —— token 已失效",
                            elapsed, round_no, errcode, data.get("msg"))
                break
            else:
                # 其它业务错误（可能是"已领取"之类），不代表鉴权失效
                logger.info("[%4.0fs] #%d ⚠️ errcode=%s msg=%s（非鉴权错误，继续观察）",
                            elapsed, round_no, errcode, data.get("msg"))
                last_ok = elapsed
        except Exception as e:
            logger.warning("[%4.0fs] #%d 请求异常: %s（网络问题，继续）", elapsed, round_no, e)

        time.sleep(INTERVAL)

    total = time.monotonic() - start
    logger.info("=" * 60)
    logger.info("探测结束，共运行 %.0fs", total)
    if first_ok is not None:
        logger.info("首次可用: +%.0fs", first_ok)
        logger.info("最后可用: +%.0fs", last_ok)
        logger.info(">>> 实测 token 存活下限: 约 %.1f 分钟 <<<", (last_ok - first_ok) / 60)
    else:
        logger.warning("全程未能成功，token 一开始就无效，或请求被彻底拦截（海外 IP 被拒？）")
    logger.info("判定参考：存活 >= 数小时 → 可考虑纯云端；仅 30~60 分钟 → 纯云端 cron 不可行")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()

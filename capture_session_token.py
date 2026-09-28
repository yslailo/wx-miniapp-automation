# -*- coding: utf-8 -*-
"""
本地（Windows）通过 WMPFDebugger 的 CDP 抓取「提现笔笔省」的 session-token。

原理与 tastin-sign/get_token.py 完全一致：
  连接 CDP proxy(ws://127.0.0.1:62000) → Network.enable → 触发页面刷新
  → 从发往 discount.wxpapp.wechatpay.cn 的请求头里截取 session-token。

前置条件：
  1. WMPFDebugger 已启动（frida hook 已加载，端口 62000 在监听）
  2. 微信中已打开「提现笔笔省」小程序（并停在领券页）
     ★ 必须先启动 WMPFDebugger 再打开小程序，hook 只对新打开的小程序生效

用法：
  pip install websocket-client
  python capture_session_token.py

输出：
  SESSION_TOKEN=xxx        （同时写入同目录 .env）
"""

import os
import sys
import time
import json
import threading
from pathlib import Path

# Windows 控制台默认 GBK，emoji（✅/❌/⚠️）会触发 UnicodeEncodeError 导致脚本中断；
# 把输出流的编码错误策略改为 replace，保证中文正常、emoji 降级为 ? 而不崩溃。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

try:
    import websocket
except ImportError:
    print("[ERROR] 需要安装 websocket-client: pip install websocket-client")
    sys.exit(1)

CDP_PORT = 62000
TARGET_HOST = "discount.wxpapp.wechatpay.cn"
LISTEN_SECONDS = int(os.environ.get("LISTEN_SECONDS", "15"))
ENV_PATH = Path(__file__).resolve().parent / ".env"
ENV_KEY = "SESSION_TOKEN"


def capture_via_cdp() -> str | None:
    """连接 CDP，触发页面刷新，从请求头截取 session-token。"""
    result = {"token": None}

    def on_message(ws, data):
        try:
            msg = json.loads(data)
        except json.JSONDecodeError:
            return
        if msg.get("method") == "Network.requestWillBeSent":
            req = msg.get("params", {}).get("request", {})
            url = req.get("url", "")
            if TARGET_HOST not in url:
                return
            headers = req.get("headers", {})
            # 头部大小写不固定，统一小写取
            token = ""
            for k, v in headers.items():
                if k.lower() == "session-token":
                    token = v
                    break
            if token and not result["token"]:
                result["token"] = token
                print(f"[CDP] 已截获 session-token ({len(token)} chars) from {url[:80]}")

    def on_open(ws):
        ws.send(json.dumps({"id": 1, "method": "Network.enable", "params": {}}))
        ws.send(json.dumps({"id": 2, "method": "Page.enable", "params": {}}))

    def on_error(ws, error):
        print(f"[CDP] 错误: {error}")

    url = f"ws://127.0.0.1:{CDP_PORT}"
    ws = websocket.WebSocketApp(
        url, on_message=on_message, on_open=on_open, on_error=on_error
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

    time.sleep(2)  # 等待连接建立
    try:
        ws.send(json.dumps({"id": 3, "method": "Page.reload", "params": {}}))
        print("[CDP] 已触发小程序页面刷新，等待请求...")
    except Exception:
        pass

    for _ in range(LISTEN_SECONDS * 2):
        if result["token"]:
            break
        time.sleep(0.5)

    ws.close()
    return result["token"]


def verify_and_report(token: str) -> bool:
    """用抓到的 token 调一次查询接口，确认新鲜有效。"""
    os.environ["SESSION_TOKEN"] = token
    try:
        import requests
        from main import build_headers, QUERY_URL, parse_response
    except Exception as e:  # noqa: BLE001
        print(f"[verify] 无法导入校验模块: {e}")
        return False

    try:
        resp = requests.get(QUERY_URL, headers=build_headers(), timeout=15, verify=False)
        data = parse_response(resp)
    except Exception as e:  # noqa: BLE001
        print(f"[verify] 校验请求失败: {e}")
        return False

    errcode = data.get("errcode")
    if errcode == 0:
        print(f"[verify] ✅ token 有效（errcode=0）")
        return True
    if errcode == 268566816:
        print(f"[verify] ❌ token 已失效（errcode=268566816）")
        return False
    print(f"[verify] ⚠️ 返回 errcode={errcode} msg={data.get('msg')}（非鉴权错误，token 大概率有效）")
    return True


def write_env(token: str) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith(f"{ENV_KEY}="):
            out.append(f"{ENV_KEY}={token}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{ENV_KEY}={token}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"[env] 已写入 {ENV_PATH}")


def main():
    print("=" * 56)
    print("  提现笔笔省 session-token 抓取（WMPFDebugger CDP）")
    print(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 56)

    token = capture_via_cdp()
    if not token:
        print("\n[ERROR] 未捕获到 session-token，请确认：")
        print("  1. WMPFDebugger 已启动（端口 62000 在监听）")
        print("  2. 「提现笔笔省」小程序已在微信中打开（且是先启动 debugger 后打开的）")
        sys.exit(1)

    verify_and_report(token)
    write_env(token)

    print("\n" + "=" * 56)
    print("  下一步：把 token 写入 GitHub Secret 并触发一次运行")
    print("=" * 56)
    print(f"  SESSION_TOKEN={token}")
    print()
    print("  方式一（gh CLI）：")
    print("    $env:SESSION_TOKEN=\"<上面的值>\"; gh secret set SESSION_TOKEN")
    print("    gh workflow run daily-claim.yml")
    print()
    print("  方式二（无 gh，用 PAT）：")
    print("    $env:GH_PAT=\"ghp_xxx\"; $env:GH_REPO=\"owner/repo\"")
    print("    python tools/push_token.py")
    print()


if __name__ == "__main__":
    main()

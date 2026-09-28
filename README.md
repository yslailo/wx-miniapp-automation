# wx-miniapp-automation

微信支付「提现笔笔省」每日自动领券 —— GitHub Actions 定时执行，免服务器。

通过逆向小程序接口，模拟小程序请求自动领取每日免费提现券。

## 功能

- **GitHub Actions 定时执行**：每天北京时间 06:00 自动运行，支持手动触发
- **token 寿命实验**：日志输出 token 指纹（`[probe] token_fp=...`）与退出码，方便判断 token 存活时长
- **直连优先 + 代理池回退**：阿里云 WAF 拦截海外 IP 时，自动切换到国内免费代理重试
  （改进版 [freeproxy fork](https://github.com/LeapYa/freeproxy)：ip2region 本地定位 + 找到可用即停）
- **一键抓 token**：本地通过 WMPFDebugger 的 CDP 自动截取 `session-token`，无需手动抓包
- **可选 mitmproxy 续期**：内置 addon，正常用小程序即可持续刷新 token

## 流程

1. `GET  /txbbs-mall/coupon/querydailygiftcoupons` —— 查询当日券信息
   （`coupon_id` / `face_value` / `exposure_token` / `is_claimed`）
2. 若 `is_claimed: true` 直接退出
3. 否则调 `POST /txbbs-mall/coupon/claimdailygiftcoupon` 领取

> 服务端对成功响应使用**双层压缩**（外层 zlib + 内层 raw deflate），`requests` 只解外层，
> 脚本在 JSON 解析失败时会做一次 raw deflate 再解析。

## 退出码

| 码 | 含义 |
|----|------|
| 0 | 成功（含"今日已领取"） |
| 1 | 业务失败 / 网络失败（可重试） |
| 2 | `SESSION_TOKEN` 已失效，需重新抓取并更新 Secret |

## 快速开始

### 1. 抓取 session-token（Windows 本地）

需要先搭好 [WMPFDebugger](https://github.com/evi0s/WMPFDebugger)（Frida hook 微信 PC 小程序运行时）：

```powershell
pip install websocket-client
python capture_session_token.py
```

前置：**先**启动 WMPFDebugger（端口 62000 在听），**再**在微信打开「提现笔笔省」并停在领券页。
脚本会触发页面刷新、截取 `session-token`、校验有效性，并写入 `.env`。

> 备选：`mitm_addon.py`（mitmproxy）——`pip install mitmproxy` → `mitmdump -s mitm_addon.py`
> → 系统代理指向 `127.0.0.1:8080` → 打开小程序，token 自动写入 `.env`。

### 2. 写入 Secret 并触发

有 `gh` CLI：

```powershell
$tok = (Select-String -Path .env -Pattern '^SESSION_TOKEN=').Line -replace '^SESSION_TOKEN=',''
$tok | gh secret set SESSION_TOKEN -R <owner>/<repo>
gh workflow run daily-claim.yml -R <owner>/<repo>
```

没有 `gh`（用 PAT）：

```powershell
$env:GH_PAT  = "github_pat_xxx"   # 权限：Actions RW + Secrets RW
$env:GH_REPO = "<owner>/<repo>"
pip install pynacl
python tools/push_token.py        # 自动读 .env，写 Secret 并触发
```

### 3. 让 Actions 自动跑

Secrets 配好后，`每日领取提现券` 会在每天北京时间 06:00 自动运行。

## Secrets

| Secret | 必填 | 说明 |
|--------|------|------|
| `SESSION_TOKEN` | 是 | 小程序 session-token |
| `USE_PROXY` | 否 | 设为 `1` 强制走国内代理（默认直连优先、被拦才回退） |

## 文件说明

| 文件 | 用途 |
|------|------|
| `main.py` | 主脚本（查询 + 领取） |
| `net.py` | 统一请求层：直连优先 + 惰性代理池回退 |
| `proxy_pool.py` | 国内免费代理池（直连被 WAF 拦时启用） |
| `capture_session_token.py` | 本地 CDP 抓取 session-token |
| `probe_token_ttl.py` | 探测 token 真实寿命与是否轮换 |
| `mitm_addon.py` | mitmproxy addon：正常使用小程序时自动续 token |
| `tools/push_token.py` | 无 gh CLI 时写 Secret 并触发运行 |
| `.github/workflows/daily-claim.yml` | Actions 定时任务 |

## 免责声明

本项目仅供学习交流使用，请勿用于商业用途或违反平台规则。使用本项目产生的一切后果由使用者自行承担。

## License

MIT

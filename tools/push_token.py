# -*- coding: utf-8 -*-
"""
本地：把 SESSION_TOKEN 写入仓库 Secret，并立即触发 daily-claim.yml 运行一次。
适用于没装 gh CLI 的环境（用 Personal Access Token + REST API）。

依赖：
  pip install requests pynacl

PAT 权限（fine-grained，仅限目标仓库）：
  · Actions: Read and write   （触发 workflow_dispatch）
  · Secrets: Read and write   （更新 Secret）

用法（PowerShell）：
  $env:GH_PAT  = "github_pat_xxx"
  $env:GH_REPO = "owner/repo"
  python tools/push_token.py                 # 自动从 ../.env 读 SESSION_TOKEN
  python tools/push_token.py --token "xxx"   # 或显式传入
  python tools/push_token.py --ref main       # 指定分支，默认 main

会在触发前打印 UTC 时间，便于第二天对比日志推算 token 存活时长。
"""

import os
import sys
import base64
import argparse
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from nacl import encoding, public
except ImportError:
    print("[ERROR] 缺少 pynacl，请先: pip install pynacl")
    sys.exit(1)

API = "https://api.github.com"
SECRET_NAME = "SESSION_TOKEN"
WORKFLOW_FILE = "daily-claim.yml"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def gh_headers(pat: str) -> dict:
    return {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def load_token(arg_token: str | None) -> str:
    if arg_token:
        return arg_token.strip()
    tok = os.environ.get("SESSION_TOKEN", "").strip()
    if tok:
        return tok
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("SESSION_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def update_secret(repo: str, pat: str, value: str) -> None:
    h = gh_headers(pat)
    r = requests.get(f"{API}/repos/{repo}/actions/secrets/public-key", headers=h, timeout=15)
    r.raise_for_status()
    key = r.json()

    pk = public.PublicKey(key["key"].encode("utf-8"), encoding.Base64Encoder())
    sealed = public.SealedBox(pk).encrypt(value.encode("utf-8"))
    encrypted = base64.b64encode(sealed).decode("utf-8")

    r = requests.put(
        f"{API}/repos/{repo}/actions/secrets/{SECRET_NAME}",
        headers=h,
        json={"encrypted_value": encrypted, "key_id": key["key_id"]},
        timeout=15,
    )
    r.raise_for_status()
    print(f"[secret] 已更新 {repo} 的 {SECRET_NAME}")


def dispatch(repo: str, pat: str, ref: str) -> None:
    h = gh_headers(pat)
    r = requests.post(
        f"{API}/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/dispatches",
        headers=h,
        json={"ref": ref},
        timeout=15,
    )
    r.raise_for_status()
    print(f"[dispatch] 已触发 {WORKFLOW_FILE}（ref={ref}，约 5~30s 后开跑）")
    print(f"[dispatch] 运行列表: https://github.com/{repo}/actions/workflows/{WORKFLOW_FILE}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", help="session-token（不传则从环境变量或 .env 读取）")
    ap.add_argument("--repo", help="owner/repo（默认取 GH_REPO 环境变量）")
    ap.add_argument("--ref", default="main", help="分支名，默认 main")
    ap.add_argument("--secret-only", action="store_true", help="只更新 Secret，不触发运行")
    args = ap.parse_args()

    pat = os.environ.get("GH_PAT", "").strip()
    repo = (args.repo or os.environ.get("GH_REPO", "")).strip()
    if not pat:
        print("[ERROR] 缺少 GH_PAT 环境变量")
        sys.exit(1)
    if not repo:
        print("[ERROR] 缺少仓库名：--repo owner/repo 或 GH_REPO 环境变量")
        sys.exit(1)

    token = load_token(args.token)
    if not token:
        print("[ERROR] 未找到 token：--token / SESSION_TOKEN / ../.env")
        sys.exit(1)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[info] utc={now} repo={repo} token_len={len(token)} token_fp={token[:6]}...")

    update_secret(repo, pat, token)
    if not args.secret_only:
        dispatch(repo, pat, args.ref)
    print("[done] 完成。第二天查看 Actions 运行日志，对比 [probe] token_fp 与 errcode 即可判断 token 寿命。")


if __name__ == "__main__":
    main()

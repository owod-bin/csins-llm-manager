#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""敏感信息扫描 —— 防止密钥 / 内网 IP / 本机绝对路径被误提交到公开仓库。

用法::

    python scripts/check_secrets.py              # 扫描 git 已跟踪的文件（默认）
    python scripts/check_secrets.py --all        # 扫描工作区所有文件（跳过 .git 与被忽略项）
    python scripts/check_secrets.py FILE [FILE…] # 只扫描指定文件

退出码：发现 **高危** 项返回 1（CI 以此作为门禁），仅中危项返回 0。

高危（HIGH）—— 确定不能进公开仓库的内容：
    * 各家 API Key / Token 的典型前缀（OpenAI、GitHub、AWS、Slack 等）
    * PEM 私钥块
    * 内网 / 保留网段 IP（10/8、172.16/12、192.168/16）

中危（WARN）—— 需要人工确认：
    * 本机绝对路径（``C:\\...`` / ``/home/xxx``），会泄露目录结构与用户名
    * 常见弱口令字样
"""
import argparse
import os
import re
import subprocess
import sys

HIGH = {
    "OpenAI 风格密钥": re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    "GitHub Token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "AWS Access Key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack Token": re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    "私钥块": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "内网 / 保留 IP": re.compile(
        r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"
    ),
}

WARN = {
    "本机绝对路径": re.compile(r"[A-Za-z]:[\\/](?:Users|Modl|AI Model|Een)[\\/]"),
    "弱口令字样": re.compile(r"\b(?:admin123|password123|12345678)\b"),
}

# 命中即放行的白名单：这些是文档里的中性示例或占位符
ALLOW_LINE = re.compile(
    r"(REPLACE_ME|YOUR_|example\.com|127\.0\.0\.1|0\.0\.0\.0|localhost|"
    r"10\.0\.0\.\*|192\.168\.x\.x|C:\\\\llama\.cpp|\\bipaddress\\.)"
)

BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".7z", ".rar", ".exe", ".dll", ".so", ".dylib", ".gguf", ".bin", ".woff",
    ".woff2", ".ttf", ".mp4", ".mp3", ".pyc",
}

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}


def git_files(root):
    """返回 git 已跟踪的文件（相对路径列表）；不在 git 仓库中则返回 None。"""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root, capture_output=True, check=True,
        ).stdout.decode("utf-8", "replace")
    except Exception:
        return None
    return [p for p in out.split("\0") if p]


def walk_files(root):
    """遍历工作区文件，跳过 .git 等目录。"""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            found.append(os.path.relpath(full, root))
    return found


def scan(root, rel_paths):
    high_hits, warn_hits, scanned = [], [], 0
    for rel in rel_paths:
        if os.path.splitext(rel)[1].lower() in BINARY_EXT:
            continue
        full = os.path.join(root, rel)
        if not os.path.isfile(full):
            continue
        try:
            text = open(full, "r", encoding="utf-8", errors="strict").read()
        except (UnicodeDecodeError, OSError):
            continue          # 二进制或不可读，跳过
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            if ALLOW_LINE.search(line):
                continue
            for label, pat in HIGH.items():
                m = pat.search(line)
                if m:
                    high_hits.append((rel, lineno, label, m.group(0)[:60]))
            for label, pat in WARN.items():
                m = pat.search(line)
                if m:
                    warn_hits.append((rel, lineno, label, m.group(0)[:60]))
    return high_hits, warn_hits, scanned


def main():
    ap = argparse.ArgumentParser(description="敏感信息扫描")
    ap.add_argument("files", nargs="*", help="只扫描指定文件")
    ap.add_argument("--all", action="store_true", help="扫描工作区所有文件（而非仅 git 跟踪的文件）")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)

    if args.files:
        targets = args.files
        source = "命令行指定"
    elif args.all:
        targets = walk_files(root)
        source = "工作区全部文件"
    else:
        targets = git_files(root)
        if targets is None:
            print("! 不在 git 仓库中，自动回退为 --all")
            targets = walk_files(root)
            source = "工作区全部文件"
        else:
            source = "git 已跟踪文件"

    high_hits, warn_hits, scanned = scan(root, targets)

    print("=" * 68)
    print("敏感信息扫描 · 来源：%s · 共扫描 %d 个文本文件" % (source, scanned))
    print("=" * 68)

    if warn_hits:
        print("\n[中危] 需人工确认（不会导致 CI 失败）：")
        for rel, ln, label, frag in warn_hits:
            print("  %s:%d  %s  ->  %s" % (rel, ln, label, frag))

    if high_hits:
        print("\n[高危] 必须处理（CI 将失败）：")
        for rel, ln, label, frag in high_hits:
            print("  %s:%d  %s  ->  %s" % (rel, ln, label, frag))
        print("\n结论：发现 %d 处高危内容，请脱敏后重新提交。" % len(high_hits))
        return 1

    print("\n结论：未发现高危内容。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

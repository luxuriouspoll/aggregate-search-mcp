"""手动扮演 MCP 客户端，与 aggregate-search server 完成一次完整对话。

用法（在 search-mcp 目录下）：
    .venv/Scripts/python.exe mcp_handshake_demo.py

它做的事和 ZCode 注册一个 MCP server 之后做的事一模一样：
1. 用 stdio 子进程方式拉起 server.py（读 config.json 里的 command/args，逻辑相同）
2. 发 initialize 握手，协商协议版本、交换能力
3. 发 notifications/initialized 通知握手完成
4. 发 tools/list 问"你有哪些工具"
5. 发 tools/call 真的调用一次 web_search
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYTHON = HERE / ".venv" / "Scripts" / "python.exe"
SERVER = HERE / "server.py"

# Windows 管道默认 GBK，统一 UTF-8 避免输出乱码/编码报错（server.py 对 stderr 同理）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def send(proc: subprocess.Popen, message: dict) -> None:
    """往 server 的 stdin 写一行 JSON——这就是 MCP stdio 传输的全部。"""
    line = json.dumps(message, ensure_ascii=False)
    print(f"-->> {line}")
    proc.stdin.write(line + "\n")
    proc.stdin.flush()


def recv(lines: queue.Queue, timeout: float = 30.0) -> dict:
    """从 server 的 stdout 读一行 JSON。带 id 的是响应；不带的是通知，跳过。"""
    while True:
        line = lines.get(timeout=timeout)
        if line is None:
            raise RuntimeError("server 提前退出（去看它 stderr 打了什么）")
        msg = json.loads(line)
        if "id" in msg:
            print(f"<<-- {line}")
            return msg
        print(f"<<-- (通知，无需回复) {line}")


def main() -> None:
    proc = subprocess.Popen(
        [str(PYTHON), str(SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,  # 单独收 stderr，最后展示——协议通道和日志通道是分开的
        cwd=str(HERE),
        encoding="utf-8",
    )
    lines: queue.Queue = queue.Queue()

    def reader() -> None:
        for line in proc.stdout:
            lines.put(line.rstrip("\n"))
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()

    print("=== 1. initialize 握手：报版本、报能力 ===")
    send(proc, {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "manual-demo", "version": "0.0.1"},
        },
    })
    resp = recv(lines)
    result = resp["result"]
    print(f"    → 协商定下的协议版本: {result['protocolVersion']}")
    print(f"    → server 自报家门: {result['serverInfo']}")
    print(f"    → server 广播的能力: {list(result['capabilities'])}")

    print("\n=== 2. notifications/initialized：握手完成的通知（不带 id，不用等回复）===")
    send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    print("\n=== 3. tools/list：ZCode 靠这一步知道你有哪些工具 ===")
    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    resp = recv(lines)
    for tool in resp["result"]["tools"]:
        print(f"    → 工具 {tool['name']}: {tool['description'][:40]}…")

    print("\n=== 4. tools/call：真的调用一次 web_search ===")
    send(proc, {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "web_search",
            "arguments": {"query": "Model Context Protocol 是什么", "max_results": 2},
        },
    })
    resp = recv(lines, timeout=60.0)
    text = resp["result"]["content"][0]["text"]
    print("    → 工具返回的文本（节选前 500 字）:")
    print("    " + text[:500].replace("\n", "\n    "))

    proc.stdin.close()
    proc.wait(timeout=10)
    print("\n=== 5. server 打到 stderr 的日志（注意：从没混进上面的协议通道）===")
    print(proc.stderr.read().strip())


if __name__ == "__main__":
    main()

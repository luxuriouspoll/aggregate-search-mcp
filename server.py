"""兼容入口：旧版的 MCP 客户端配置是 `python /路径/server.py`，这个文件让它继续可用。

新安装请直接用 `aggregate-search` 命令（见 README），不需要这个文件。
"""
from aggregate_search.cli import main

if __name__ == "__main__":
    main()

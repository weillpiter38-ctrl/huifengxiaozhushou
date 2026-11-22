"""单一版本号来源。

`APP_VERSION` 只在这里定义一次，`smm/api.py`（版本牌、更新清单比对）、
`smm/net.py`（User-Agent）、`smm/server.py`（健康检查）都从这里取。
发布时只需改这一个常量 + `build/version_info.txt` + 前端 `?v=` 串，
`tests/test_net_ua.py` 会盯着 User-Agent 别再滞留旧版本。
"""

APP_NAME = "Huifeng's Mod Manager"
APP_VERSION = "2.5.22"

__all__ = ["APP_NAME", "APP_VERSION"]

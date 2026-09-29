"""keyset 游标分页（§6.4）：cursor 为上一页末行排序键的不透明 base64url 编码。

一律 keyset、禁用 OFFSET；cursor 内含排序键值 + entry_id，反转即抛 ValueError
（由路由层转 400）。
"""

import base64
import binascii
import json
from typing import Any


def encode_cursor(key: Any, last_id: int) -> str:
    payload = json.dumps({"k": key, "id": last_id}, ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> "tuple[Any, int]":
    """解析 cursor → (排序键值, entry_id)。非法编码抛 ValueError。"""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        obj = json.loads(raw)
        key, last_id = obj["k"], obj["id"]
        if not isinstance(last_id, int):
            raise ValueError("cursor.id 必须为整数")
        return key, last_id
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        raise ValueError(f"非法 cursor：{e}") from e

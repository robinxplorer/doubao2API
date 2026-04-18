import os
import json
from pathlib import Path
from pydantic_settings import BaseSettings
from typing import Dict, Optional

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    # 服务配置
    PORT: int = int(os.getenv("PORT", 7861))
    WORKERS: int = int(os.getenv("WORKERS", 3))
    ADMIN_KEY: str = os.getenv("ADMIN_KEY", "admin")
    REGISTER_SECRET: str = os.getenv("REGISTER_SECRET", "")

    # 引擎模式：doubao 仅支持 browser（a_bogus/msToken 由浏览器 JS 拦截器自动注入）
    ENGINE_MODE: str = os.getenv("ENGINE_MODE", "browser")

    # 浏览器引擎配置（Playwright + Chromium）
    BROWSER_POOL_SIZE: int = int(os.getenv("BROWSER_POOL_SIZE", 2))
    MAX_INFLIGHT_PER_ACCOUNT: int = int(os.getenv("MAX_INFLIGHT", 1))
    STREAM_KEEPALIVE_INTERVAL: int = int(os.getenv("STREAM_KEEPALIVE_INTERVAL", 5))

    # 容灾与限流
    MAX_RETRIES: int = 2
    EMPTY_RESPONSE_RETRIES: int = 1
    ACCOUNT_MIN_INTERVAL_MS: int = int(os.getenv("ACCOUNT_MIN_INTERVAL_MS", 1200))
    REQUEST_JITTER_MIN_MS: int = int(os.getenv("REQUEST_JITTER_MIN_MS", 120))
    REQUEST_JITTER_MAX_MS: int = int(os.getenv("REQUEST_JITTER_MAX_MS", 360))
    RATE_LIMIT_BASE_COOLDOWN: int = int(os.getenv("RATE_LIMIT_BASE_COOLDOWN", 600))
    RATE_LIMIT_MAX_COOLDOWN: int = int(os.getenv("RATE_LIMIT_MAX_COOLDOWN", 3600))
    RATE_LIMIT_COOLDOWN: int = RATE_LIMIT_BASE_COOLDOWN

    # 数据文件路径
    ACCOUNTS_FILE: str = os.getenv("ACCOUNTS_FILE", str(DATA_DIR / "accounts.json"))
    USERS_FILE: str = os.getenv("USERS_FILE", str(DATA_DIR / "users.json"))
    SESSIONS_FILE: str = os.getenv("SESSIONS_FILE", str(DATA_DIR / "sessions.json"))

    # 豆包特有配置
    BASE_URL: str = os.getenv("BASE_URL", "https://www.doubao.com")
    DEFAULT_BOT_ID: str = os.getenv("DEFAULT_BOT_ID", "7338286299411103781")
    DEFAULT_FP: str = os.getenv("DEFAULT_FP", "doubao2api_default_fp")

    class Config:
        env_file = ".env"


API_KEYS_FILE = DATA_DIR / "api_keys.json"


def load_api_keys() -> set:
    if API_KEYS_FILE.exists():
        try:
            with open(API_KEYS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("keys", []))
        except Exception:
            pass
    return set()


def save_api_keys(keys: set):
    API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(API_KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump({"keys": list(keys)}, f, indent=2)


# 在内存中存储管理的 API Keys
API_KEYS = load_api_keys()

VERSION = "1.0.0"

settings = Settings()

# ── 模型映射 ──────────────────────────────────────────────
# 豆包用 bot_id 标识模型，而非千问的 model 字段
# bot_id 来源：F12 抓包 / Network / chat/completion 请求体

BOT_MAP: Dict[str, str] = {
    # 默认对话模型（豆包默认）
    "doubao":                  "7338286299411103781",
    "doubao-pro":              "7338286299411103781",
    "doubao-lite":             "7338286299411103781",
    # OpenAI 兼容别名
    "gpt-4o":                  "7338286299411103781",
    "gpt-4o-mini":             "7338286299411103781",
    "gpt-4-turbo":             "7338286299411103781",
    "gpt-4":                   "7338286299411103781",
    "gpt-4.1":                 "7338286299411103781",
    "gpt-4.1-mini":            "7338286299411103781",
    "gpt-3.5-turbo":           "7338286299411103781",
    "gpt-5":                   "7338286299411103781",
    "o1":                      "7338286299411103781",
    "o1-mini":                 "7338286299411103781",
    "o3":                      "7338286299411103781",
    "o3-mini":                 "7338286299411103781",
    # Anthropic
    "claude-opus-4-6":         "7338286299411103781",
    "claude-sonnet-4-6":       "7338286299411103781",
    "claude-sonnet-4-5":       "7338286299411103781",
    "claude-3-opus":           "7338286299411103781",
    "claude-3-5-sonnet":       "7338286299411103781",
    "claude-3-5-sonnet-latest": "7338286299411103781",
    "claude-3-sonnet":         "7338286299411103781",
    "claude-3-haiku":          "7338286299411103781",
    "claude-3-5-haiku":        "7338286299411103781",
    "claude-3-5-haiku-latest": "7338286299411103781",
    "claude-haiku-4-5":        "7338286299411103781",
    # Gemini
    "gemini-2.5-pro":          "7338286299411103781",
    "gemini-2.5-flash":        "7338286299411103781",
    "gemini-1.5-pro":          "7338286299411103781",
    "gemini-1.5-flash":        "7338286299411103781",
    # DeepSeek
    "deepseek-chat":           "7338286299411103781",
    "deepseek-reasoner":       "7338286299411103781",
}

# 旧版 MODEL_MAP 保持兼容，映射到 bot_id
MODEL_MAP = BOT_MAP


def resolve_bot_id(name: str) -> str:
    """将 OpenAI 兼容模型名或豆包别名解析为 bot_id。

    查找顺序：
    1. BOT_MAP 精确匹配
    2. 如果 name 本身就是纯数字 bot_id，直接返回
    3. 兜底返回 DEFAULT_BOT_ID
    """
    if name in BOT_MAP:
        return BOT_MAP[name]
    # 纯数字 → 直接当 bot_id
    if name.isdigit():
        return name
    return settings.DEFAULT_BOT_ID

"""配置模块：集中从环境变量加载所有外部依赖配置，代码中绝不硬编码任何密钥。

对外主要提供：
- get_settings()   : 全局唯一 Settings 实例（lru_cache 缓存）
- build_llm()      : 按 LLM_PROVIDER 创建 ChatOpenAI 实例（OpenAI 兼容接口）
"""
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic_settings import BaseSettings, SettingsConfigDict

# 显式加载项目根目录下的 .env（无论从哪个 cwd 启动）
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_PROJECT_ROOT / ".env")


class Settings(BaseSettings):
    """从环境变量读取的全部运行时配置。字段名大写即对应环境变量名。"""

    # ---------- LLM 提供方 ----------
    # 可选: deepseek（默认） | siliconflow
    llm_provider: Literal["deepseek", "siliconflow"] = "deepseek"

    # DeepSeek 官方 API（OpenAI 兼容端点）
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-v4-flash"

    # 硅基流动 SiliconFlow（OpenAI 兼容端点）
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_model: str = "deepseek-ai/DeepSeek-V3.2"

    # ---------- Tavily 搜索（国外源）----------
    # 为空时该源不启用（search_web 聚合时跳过）
    tavily_api_key: str = ""
    tavily_max_results: int = 5

    # ---------- 博查搜索（国内源，可选）----------
    # 为空时不启用；配置后与 Tavily 结果合并，覆盖国内互联网内容
    bocha_api_key: str = ""
    bocha_base_url: str = "https://api.bocha.cn/v1/web-search"

    # ---------- LangGraph Checkpointer 持久化 ----------
    # 可选: memory（进程内） | sqlite（文件持久化） | postgres（生产，需 DATABASE_URL）
    checkpointer: Literal["memory", "sqlite", "postgres"] = "sqlite"
    checkpoint_db: str = "./data/checkpoints.sqlite"
    # PostgreSQL 连接串（CHECKPOINTER=postgres 时必填）
    database_url: str = ""

    # ---------- 调研历史持久化 ----------
    # 历史功能（列表 / 批改意见）使用的独立 SQLite 文件，与 checkpoint 分离
    history_db: str = "./data/history.sqlite"

    # ---------- 可观测性（可选）----------
    langchain_tracing_v2: bool = False
    langchain_api_key: str = ""
    langchain_project: str = "AutoResearch"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """返回全局唯一的 Settings 实例（进程内缓存）。"""
    return Settings()


def build_llm() -> ChatOpenAI:
    """按 LLM_PROVIDER 创建 ChatOpenAI 实例，统一走 OpenAI 兼容接口。

    留好了接口：更换模型只需修改 .env 中对应 provider 的
    *_BASE_URL / *_MODEL / *_API_KEY，无需改动业务代码。
    """
    settings = get_settings()
    common = {"temperature": 0.3, "max_retries": 2}

    if settings.llm_provider == "siliconflow":
        return ChatOpenAI(
            model=settings.siliconflow_model,
            api_key=settings.siliconflow_api_key,
            base_url=settings.siliconflow_base_url,
            **common,
        )

    # 默认走 DeepSeek 官方 API
    return ChatOpenAI(
        model=settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        **common,
    )

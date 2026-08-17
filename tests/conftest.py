"""pytest 共享配置：将 backend 目录加入导入路径，并默认跳过集成测试。"""
import sys
from pathlib import Path

import pytest

# 使测试可直接 import backend 下的模块
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))


def pytest_collection_modifyitems(config, items):
    """默认跳过 integration 标记的测试，除非显式 `pytest -m integration`。

    集成测试会调用真实 LLM（消耗 API 额度），默认不应在普通测试时误跑。
    """
    if config.getoption("-m") == "integration":
        return
    skip_integration = pytest.mark.skip(reason="集成测试，请使用 `pytest -m integration` 运行")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)

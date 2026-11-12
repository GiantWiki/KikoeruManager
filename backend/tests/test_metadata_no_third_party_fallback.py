"""元数据链路只允许 DLsite 两条（product_info / 直连 product.json）。

voicehub.top 兜底已在 v2.6.74 移除：它从 2026-09 起整体迁站到 asmrdaily.drive2dav.top，
`/api/*` 需要登录鉴权（401），旧地址只会拿到 HTML 或超时，每次失败白等 30~60 秒，
还把「DLsite 连不上」伪装成「元数据源都没有这部作品」。这里钉住行为，防止有人
再把未经鉴权的第三方源塞回链路里。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import dlsite_service as dlsite_service_module
from app.core import metadata_service as metadata_service_module
from app.core.metadata_service import MetadataService


def _config_stub() -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(
            cache_enabled=False,
            connect_timeout=10,
            read_timeout=10,
            http_proxy=None,
        )
    )


def _task_stub() -> SimpleNamespace:
    return SimpleNamespace(
        rjcode="RJ01685546",
        task_metadata={},
        update_progress=lambda *args, **kwargs: None,
    )


@pytest.mark.asyncio
async def test_fetch_stops_after_dlsite_chains_fail(tmp_path, monkeypatch):
    work_dir = tmp_path / "RJ01685546"
    work_dir.mkdir()

    monkeypatch.setattr(metadata_service_module, "get_config", lambda: _config_stub())
    monkeypatch.setattr(
        dlsite_service_module,
        "recent_dlsite_transport_failure",
        lambda **kwargs: "",
    )

    service = MetadataService()
    service._fetch_from_dlsite_product_info = AsyncMock(return_value=None)
    service._fetch_from_dlsite = AsyncMock(return_value=None)

    payload = await service.fetch(str(work_dir), _task_stub(), force_refresh=True)

    # 两条 DLsite 链路都试过，且没有第三条外部链路
    assert service._fetch_from_dlsite_product_info.await_count == 1
    assert service._fetch_from_dlsite.await_count == 1
    assert payload["metadata_source"] == "minimal"
    # 数据侧终态（没有传输失败快照）→ 文案指向「源里没有这部作品」
    assert payload["rename_skipped_reason"] == "所有元数据源均未找到该作品"


def test_voicehub_config_field_is_gone():
    # 配置项一并移除；老配置文件里残留该项时 pydantic 只会忽略，不会加载失败
    from app.config.settings import MetadataConfig

    assert "voicehub_enabled" not in MetadataConfig.model_fields
    assert "voicehub_enabled" not in MetadataConfig(voicehub_enabled=True).model_dump()

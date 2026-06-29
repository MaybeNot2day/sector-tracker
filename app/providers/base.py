from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import AssetConfig, Bar, ProviderName, Quote


class QuoteProvider(ABC):
    name: ProviderName

    @abstractmethod
    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        raise NotImplementedError

    @abstractmethod
    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        raise NotImplementedError


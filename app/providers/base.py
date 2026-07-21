from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from app.models import AssetConfig, Bar, ProviderName, Quote

ValidationStatus = Literal["valid", "not_found", "unavailable"]


class QuoteProvider(ABC):
    name: ProviderName

    @abstractmethod
    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        raise NotImplementedError

    @abstractmethod
    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        raise NotImplementedError

    async def validate_asset(self, asset: AssetConfig) -> ValidationStatus:
        """Classify an edit without treating an upstream outage as not-found."""
        try:
            quotes = await self.get_quotes([asset])
        except Exception:
            return "unavailable"
        return "valid" if any(quote.symbol == asset.symbol for quote in quotes) else "unavailable"

    async def aclose(self) -> None:
        """Release provider-owned resources, if any."""
        return None

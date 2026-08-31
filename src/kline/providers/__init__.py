"""Data providers — one per asset class, all return list[Candle]."""

from kline.providers.base import EntitlementBlocked, Provider
from kline.providers.ashare import AShareProvider
from kline.providers.tushare_mvp import TuShareEntitlement, TuShareMvpProvider
from kline.providers.us import USStockProvider
from kline.providers.crypto import CryptoProvider
from kline.providers.commodity import CommodityProvider
from kline.providers.binance_usdm import BinanceUsdmFuturesProvider

__all__ = [
    "Provider",
    "EntitlementBlocked",
    "AShareProvider",
    "TuShareEntitlement",
    "TuShareMvpProvider",
    "USStockProvider",
    "CryptoProvider",
    "CommodityProvider",
    "BinanceUsdmFuturesProvider",
]

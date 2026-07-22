# datafeed

## 要去哪里
多资产 K 线数据服务:ticker+timeframe → 标准化 OHLCV + provenance(provider/fresh/synthetic 标记),A股/美股/加密/商品全覆盖;近期喂养 trading-system 与 tokenpulse,远期可作为独立 API 产品外卖。

## 现在在哪里(2026-07-21)
- V1 运行中:tushare/yahoo/binance 多源,ports-and-adapters,realtime strict 失败即显式报错、永不隐藏降级。
- Park 2026-07-21 裁定为产品(非基础设施),上 Portfolio 板。
- 无排队任务;最后一次推送 2026-07-15。

## 下一步
- 按晨会需求排;若走 API 外卖路线,先立商业化合同(对外发布风险轴归 Park)。

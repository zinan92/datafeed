<div align="center">

# datafeed

**多资产 K 线数据服务 — 一个 ticker + 一个 timeframe，返回标准化 OHLCV 蜡烛图**

[![Python](https://img.shields.io/badge/python-3.11+-3776AB.svg?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

</div>

---

```
in  ticker + timeframe (1m/5m/15m/30m/1h/4h/1d/1w) + date range
out OHLCV candles + provenance header (provider, source_mode, quality_flags,
    is_synthetic=false, served_from, fresh, age_seconds, execution_venue,
    cache_policy, quality_policy, fallback_policy)

fail ticker not found     → search suggestions
cache hit                 → cached candles, tagged served_from=cache + age_seconds
realtime strict failure   → error / blocked envelope, never hidden fallback
fail timeframe not supported → supported list
fail A-share no token     → setup instructions
```

Sources: `tushare_pro`, `yahoo_finance`, `yahoo_finance_futures`, `binance_spot_public`, `binance_usdm_futures`

## Ports And Adapters

datafeed 现在按 ports-and-adapters 组织：

```text
API / consumers
   │
   ▼
MarketDataPort
   │
   ├─ SourceManifest: source id, asset class, market type, quality flags, execution venue, aliases
   ├─ fetch_candles(): REST/historical pull
   ├─ stream_candles(): realtime stream
   ├─ canonical_ticker(): adapter-owned symbol normalization
   └─ last_raw_response: raw payload for audit/debug
        │
        ├─ Binance USD-M Futures adapter
        ├─ Binance Spot adapter
        ├─ Yahoo adapter
        ├─ TuShare adapter
        └─ any new broker adapter
```

新增 broker / exchange 的接入面：

1. 实现 `MarketDataPort`，或用 `ProviderBackedMarketDataAdapter` 包一层旧 provider。
2. 提供一个 `SourceManifest`，声明：
   - `source_id`
   - `asset_class`
   - `market_type`
   - `execution_venue`
   - `realtime_supported`
   - `quality_flags`
   - `ticker_aliases`
3. 调用 `register_adapter(adapter)`。

API 主流程不需要为新 broker 改分支。请求只需要：

```bash
curl "localhost:8100/api/candles/crypto/BTC?source=fake_broker_feed&cache_policy=bypass"
```

## Source + Policy

datafeed 本体只负责数据。它不判断“研究”或“交易”，而是把每次请求的 **source + cache policy + quality policy + fallback policy** 写进响应，让消费者自己决定是否可用。

| 概念 | 参数 | 含义 |
|------|------|------|
| Source | `source=auto` 或具体 source id | 选择上游，如 `binance_usdm_futures` |
| Cache | `cache_policy=allow|bypass|require` | 是否允许、跳过、或只读 SQLite cache |
| Quality | `quality=standard|strict` | `strict` 会检查 empty/stale/gap/duplicate/out-of-order 并 blocked |
| Fallback | `fallback_policy=none|explicit` | 当前不做静默 fallback；字段保留给未来显式 fallback |
| Execution venue | `require_execution_venue=true|false` | 要求 source 必须是执行场所 |

常用 profile：

| Profile | 等价策略 | 用途 |
|---------|----------|------|
| `profile=historical` | `cache_policy=allow`, `quality=standard` | 历史分析、回放、离线图表 |
| `profile=realtime` | `cache_policy=bypass`, `quality=strict`, `fallback_policy=none` | 任意实时数据源的高标准拉取 |
| `profile=execution_live` | `source=binance_usdm_futures`, `cache_policy=bypass`, `quality=strict`, `fallback_policy=none`, `require_execution_venue=true` | XAUUSDT 执行场所实时 K 线 |

兼容参数仍然可用：

- `mode=research` 等价默认 historical 行为。
- `mode=live` 或 `strict=true` 是 `profile=execution_live` 的旧快捷方式。
- `refresh=true` 等价 `cache_policy=bypass`。

硬约束：

- `is_synthetic` 恒为 `false`，没有合成/占位数据路径。
- `cache_policy=bypass` / `profile=realtime` / `profile=execution_live` 不读 cache。
- `quality=strict` 下，上游失败、空数据、陈旧、gap、乱序都返回错误/blocked envelope。
- `require_execution_venue=true` 会拒绝 Yahoo、TuShare、Binance Spot 等非执行场所 source。

```bash
# 历史/回放：允许 cache
curl "localhost:8100/api/candles/commodity/GOLD?timeframe=1d&profile=historical"

# 任意实时源高标准：跳过 cache + strict quality
curl "localhost:8100/api/candles/crypto/BTC?timeframe=1m&source=binance_spot_public&profile=realtime"

# 执行场所实时：Binance USD-M Futures XAUUSDT
curl "localhost:8100/api/candles/commodity/XAUUSDT?timeframe=1m&profile=execution_live&limit=200"

# WebSocket 实时 candle update
ws://localhost:8100/api/ws/candles/commodity/XAUUSDT?timeframe=1m&source=binance_usdm_futures
```

## 示例输出

```bash
$ curl "localhost:8100/api/candles/us_stock/AAPL?timeframe=1d&limit=3"
```

```json
{
  "ticker": "AAPL",
  "asset_class": "us_stock",
  "timeframe": "1d",
  "count": 3,
  "schema_version": "kline-candles-v1",
  "provider": "yahoo_finance",
  "source_mode": "yahoo_finance",
  "requested_source": "auto",
  "cache_policy": "allow",
  "quality_policy": "standard",
  "fallback_policy": "none",
  "require_execution_venue": false,
  "quality_flags": ["delayed_possible", "market_hours", "research_only"],
  "is_synthetic": false,
  "served_from": "upstream",
  "fresh": null,
  "latest_timestamp": "2026-03-28",
  "age_seconds": 172800.0,
  "max_age_seconds": null,
  "execution_venue": false,
  "reject_reason": null,
  "access_issues": [],
  "candles": [
    {"timestamp": "2026-03-26", "open": 178.5, "high": 182.3, "low": 177.8, "close": 181.2, "volume": 52340000, "provider": "yahoo_finance", "quality_flags": ["delayed_possible", "market_hours", "research_only"]},
    {"timestamp": "2026-03-27", "open": 181.2, "high": 183.1, "low": 179.5, "close": 180.8, "volume": 48120000, "provider": "yahoo_finance", "quality_flags": ["delayed_possible", "market_hours", "research_only"]},
    {"timestamp": "2026-03-28", "open": 180.8, "high": 185.0, "low": 180.2, "close": 184.5, "volume": 55670000, "provider": "yahoo_finance", "quality_flags": ["delayed_possible", "market_hours", "research_only"]}
  ]
}
```

每个响应都带一层 **provenance / 信任头**，让下游（图表、回测、agent）不用猜数据来自哪、有多新、是不是真价格：

| 字段 | 含义 |
|------|------|
| `provider` / `source_mode` | 具体上游与取数路径（`binance_spot` / `yahoo_finance` / `tushare` / `binance_usdm_futures`） |
| `requested_source` | 调用方请求的 source。`auto` 会解析成 asset class 默认 source |
| `cache_policy` | `allow` 可读 cache，`bypass` 跳过 cache，`require` 只读 cache |
| `quality_policy` | `standard` 只标记质量事实；`strict` 会对 empty/stale/gap/duplicate/out-of-order blocked |
| `fallback_policy` | 当前默认 `none`，不做静默 fallback |
| `require_execution_venue` | 是否要求 source 必须是执行场所 |
| `quality_flags` | 数据性质标记；research 源带 `research_only` / `not_execution_venue`，live futures 源带 `live` / `execution_venue` |
| `is_synthetic` | 恒为 `false`。kline 没有任何合成/占位数据路径，返回的要么是真实数据、要么直接报错 |
| `served_from` | `"cache"`（本地库）、`"upstream"`（刚从上游拉的）或 `"websocket"`（实时推送） |
| `latest_timestamp` / `age_seconds` | 最新一根 bar 的时间与年龄（永远是诚实事实） |
| `fresh` / `max_age_seconds` | **仅对 7×24 连续市场（crypto）给出**新鲜度判定；行情有休市的源（美股/商品/A股）恒为 `null`，由消费方按自己的交易日历判断 |
| `execution_venue` | 是否来自可作为交易页实时真源的执行场所。research 源为 `false`，Binance USD-M Futures live 为 `true` |
| `reject_reason` / `access_issues` | strict quality blocked 时的直接原因，如 `upstream_error`、`empty_data`、`stale`、`gap`、`out_of_order` |

> `cache_policy=allow` 命中 cache 时不会自动回源刷新，但 `served_from` + `age_seconds` + `fresh` 让陈旧数据可见。实时路径应使用 `cache_policy=bypass` 或 `profile=realtime`。

```bash
# A股日线 (需要 TuShare token)
$ curl "localhost:8100/api/candles/a_share/000001?timeframe=1d"

# 加密货币 1 小时线
$ curl "localhost:8100/api/candles/crypto/BTC?timeframe=1h&limit=100"

# 商品 — 黄金日线
$ curl "localhost:8100/api/candles/commodity/GOLD?timeframe=1d"
```

## 架构

```
Request ▶ Resolve source manifest + cache/quality/fallback policy
   │
   ├─ cache_policy=require ▶ cache hit? yes: return served_from=cache
   │                            no: error cache_miss
   │
   ├─ cache_policy=allow ▶ cache hit? yes: return served_from=cache
   │                          no: fetch upstream
   │
   └─ cache_policy=bypass ▶ fetch upstream

MarketDataPort adapter ▶ raw response capture ▶ normalize candles
   │
   ├─ quality=standard ▶ return envelope with visible quality flags
   └─ quality=strict   ▶ empty/stale/gap/duplicate/out-of-order => blocked

Adapters: TuShare / Yahoo / Binance Spot / Binance USD-M Futures / registered brokers
```

## 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/zinan92/datafeed.git
cd datafeed

# 2. 安装依赖
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 3. 配置 A 股数据源 (可选，美股/加密/商品无需配置)
cp .env.example .env
# 编辑 .env 填入 TUSHARE_TOKEN (免费申请: tushare.pro)

# 4. 启动服务
python -m kline
# 访问 http://localhost:8100/docs 查看交互式 API 文档
```

## API 参考

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/candles/{asset_class}/{ticker}` | 获取 K 线蜡烛图数据；支持 source/cache/quality policy |
| `WS` | `/api/ws/candles/{asset_class}/{ticker}` | 实时 candle update；当前支持 Binance USD-M Futures |
| `GET` | `/api/tickers` | 列出本地已缓存的 ticker |
| `GET` | `/api/health` | 健康检查 + provider availability |

### 参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `asset_class` | path | — | `a_share` / `us_stock` / `crypto` / `commodity` |
| `ticker` | path | — | 代码: `000001`, `AAPL`, `BTC`, `GOLD` |
| `timeframe` | query | `1d` | `1m` / `5m` / `15m` / `30m` / `1h` / `4h` / `1d` / `1w` |
| `start` | query | — | 起始日期 `YYYY-MM-DD` |
| `end` | query | — | 结束日期 `YYYY-MM-DD` |
| `limit` | query | `500` | 返回蜡烛数量上限 (1-2000) |
| `source` | query | `auto` | `auto` / `tushare_pro` / `yahoo_finance` / `yahoo_finance_futures` / `binance_spot_public` / `binance_usdm_futures` |
| `cache_policy` | query | `allow` | `allow` / `bypass` / `require` |
| `quality` | query | `standard` | `standard` / `strict` |
| `fallback_policy` | query | `none` | `none` / `explicit`；当前没有静默 fallback |
| `require_execution_venue` | query | `false` | `true` 会拒绝非执行场所 source |
| `profile` | query | — | `historical` / `realtime` / `execution_live` |
| `refresh` | query | `false` | 兼容参数；`true` 等价 `cache_policy=bypass` |
| `mode` | query | `research` | 兼容参数；`live` 等价 `profile=execution_live` |
| `strict` | query | `false` | 兼容参数；`true` 等价 `profile=execution_live` |

### 商品别名

| 别名 | Yahoo Finance 代码 |
|------|-------------------|
| `GOLD`, `XAUUSD` | GC=F |
| `SILVER`, `XAGUSD` | SI=F |
| `OIL`, `CRUDE`, `WTI` | CL=F |
| `BRENT` | BZ=F |
| `NATGAS` | NG=F |
| `COPPER` | HG=F |
| `CORN`, `WHEAT`, `SOYBEAN` | ZC=F, ZW=F, ZS=F |

## Source Registry

| Source | Asset class | Provider | Market type | Realtime | Execution venue | 支持 Timeframe |
|--------|-------------|----------|-------------|----------|-----------------|---------------|
| `tushare_pro` | `a_share` | TuShare Pro | equity | false | false | 1d, 1w |
| `yahoo_finance` | `us_stock` | Yahoo Finance | equity | false | false | 1m, 5m, 15m, 30m, 1h, 1d, 1w |
| `yahoo_finance_futures` | `commodity` | Yahoo Finance | continuous futures contract | false | false | 1m, 5m, 15m, 30m, 1h, 1d, 1w |
| `binance_spot_public` | `crypto` | Binance Spot API | spot | true | false | 1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w |
| `binance_usdm_futures` | `commodity` | Binance USD-M Futures | USD-M futures | true | true | XAUUSDT: 1m, 5m, 15m, 30m, 1h, 4h |

## 技术栈

| 层级 | 技术 | 用途 |
|------|------|------|
| 运行时 | Python 3.11+ | 核心语言 |
| 框架 | FastAPI | REST API |
| 存储 | SQLite (WAL mode) | 本地缓存，无需外部数据库 |
| 验证 | Pydantic v2 | 请求/响应模型 |

## 项目结构

```
kline/
├── src/kline/
│   ├── app.py              # FastAPI 应用
│   ├── api.py              # 3 个 API 端点
│   ├── models.py           # Candle 数据模型 + 枚举
│   ├── ports.py            # MarketDataPort + SourceManifest
│   ├── quality.py          # stale/gap/out-of-order 检测
│   ├── store.py            # SQLite 存储 (upsert + query)
│   ├── registry.py         # Adapter 注册与初始化
│   ├── config.py           # 环境变量配置
│   └── providers/
│       ├── base.py         # Provider Protocol 接口
│       ├── ashare.py       # TuShare Pro (A股)
│       ├── us.py           # Yahoo Finance (美股)
│       ├── crypto.py       # Binance 公开 API (加密货币)
│       ├── commodity.py    # Yahoo Finance 期货 + 别名
│       └── binance_usdm.py # Binance USD-M Futures live
├── tests/                  # unit tests
├── data/                   # SQLite 数据库 (自动创建)
├── .env.example
└── pyproject.toml
```

## 配置

| 变量 | 说明 | 必填 | 默认值 |
|------|------|------|--------|
| `KLINE_TUSHARE_TOKEN` | TuShare Pro token (A股数据) | A股必填 | — |
| `KLINE_DB_PATH` | SQLite 数据库路径 | 否 | `data/kline.db` |
| `KLINE_PORT` | 服务端口 | 否 | `8100` |
| `KLINE_REQUEST_TIMEOUT` | 上游请求超时 (秒) | 否 | `30` |

## For AI Agents

### Capability Contract

```yaml
name: kline
version: 0.2.0
capability:
  summary: "Multi-asset K-line data service with explicit source/cache/quality/fallback policies."
  in: "ticker + timeframe + optional date range + source + cache_policy + quality + profile"
  out: "standardized OHLCV candles + provenance (provider, source_mode, policies, quality_flags, is_synthetic=false, served_from, fresh, age_seconds, execution_venue)"
  guarantees:
    - "is_synthetic is always false — real data or an error, never a fabricated placeholder"
    - "new brokers integrate through MarketDataPort + SourceManifest + register_adapter(adapter)"
    - "source identity and cache/quality/fallback policies are visible in every response"
    - "profile=realtime skips cache and applies strict quality checks"
    - "profile=execution_live uses Binance USD-M Futures XAUUSDT, skips cache, applies strict quality checks, and requires execution_venue=true"
    - "instrument-definition-v1 exposes upstream price/quantity constraints and reports unavailable fee or multiplier fields instead of inventing values"
  fail:
    - "ticker not found → suggestions list"
    - "cache_policy=require + no cache → cache_miss"
    - "quality=strict + source down/empty/stale/gap/out-of-order → error or blocked envelope"
    - "timeframe not supported → supported timeframes list"
    - "A-share no token → setup instructions"
  sources: [tushare_pro, yahoo_finance, yahoo_finance_futures, binance_spot_public, binance_usdm_futures]
api_base_url: http://localhost:8100
endpoints:
  - path: /api/instruments/{asset_class}/{ticker}
    method: GET
    description: "Get a versioned upstream execution instrument definition"
    params:
      - name: source
        type: string
        default: auto
      - name: require_execution_venue
        type: boolean
        default: false
  - path: /api/candles/{asset_class}/{ticker}
    method: GET
    description: "Get OHLCV candles under explicit source/cache/quality policy"
    params:
      - name: asset_class
        type: string
        enum: [a_share, us_stock, crypto, commodity]
        required: true
      - name: ticker
        type: string
        required: true
      - name: timeframe
        type: string
        enum: ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]
        default: "1d"
      - name: limit
        type: integer
        default: 500
      - name: source
        type: string
        enum: [auto, tushare_pro, yahoo_finance, yahoo_finance_futures, binance_spot_public, binance_usdm_futures]
        default: auto
      - name: cache_policy
        type: string
        enum: [allow, bypass, require]
        default: allow
      - name: quality
        type: string
        enum: [standard, strict]
        default: standard
      - name: profile
        type: string
        enum: [historical, realtime, execution_live]
  - path: /api/ws/candles/{asset_class}/{ticker}
    method: WS
    description: "Stream realtime candle updates; current stream source is Binance USD-M Futures XAUUSDT"
  - path: /api/tickers
    method: GET
    description: "List cached tickers"
  - path: /api/health
    method: GET
    description: "Health check"
install_command: "pip install -e ."
start_command: "python -m kline"
health_check: "GET /api/health"
```

### Agent 调用示例

```python
import httpx

async def get_candles(ticker: str, asset_class: str = "us_stock", timeframe: str = "1d"):
    """获取任意资产的 K 线数据"""
    base = "http://localhost:8100"
    resp = await httpx.AsyncClient().get(
        f"{base}/api/candles/{asset_class}/{ticker}",
        params={"timeframe": timeframe, "limit": 100},
    )
    return resp.json()["candles"]

# 美股
candles = await get_candles("AAPL")

# A股
candles = await get_candles("000001", "a_share")

# 加密货币 1 小时线
candles = await get_candles("BTC", "crypto", "1h")

# 黄金
candles = await get_candles("GOLD", "commodity")

# 执行场所实时：XAUUSDT Binance USD-M Futures，禁止 cache/fallback
async with httpx.AsyncClient() as client:
    resp = await client.get(
        "http://localhost:8100/api/candles/commodity/XAUUSDT",
        params={"timeframe": "1m", "profile": "execution_live", "limit": 200},
    )
    resp.raise_for_status()
    live_candles = resp.json()["candles"]
```

## 相关项目

| 项目 | 说明 | 链接 |
|------|------|------|
| quant-data-pipeline | 原始全功能量化数据平台 (kline 从中拆出) | [zinan92/quant-data-pipeline](https://github.com/zinan92/quant-data-pipeline) |
| trading-copilot | AI 交易分析终端 (44 种方法论) | [zinan92/trading-copilot](https://github.com/zinan92/trading-copilot) |

## License

MIT

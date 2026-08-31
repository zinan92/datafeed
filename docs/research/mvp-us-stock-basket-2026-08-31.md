# 美股 K 线数据库 MVP：固定 30 只股票样本

> 调研日期：2026-08-31  
> 用途：验证真实市场数据数据库的采集、补洞、公司行动、标识映射、查询和恢复；**不是投资建议，也不是选股推荐**。  
> 范围：美国交易所上市的公司证券；不含 ETF、基金、权证、优先股、低价股和指数产品。

## 1. 结论

建议将美股 MVP 固定为以下 **30 只**：

`NVDA, TSLA, AAPL, MSFT, AMZN, GOOGL, META, AMD, AVGO, INTC, PLTR, NFLX, CRM, JPM, BAC, V, WMT, COST, KO, PG, LLY, JNJ, UNH, XOM, CVX, F, DIS, CAT, BRK.B, TSM`

这个 basket 不是按某一天成交量机械取前 30。那样会被低价股、单日异动和不可用证券类型污染。这里采用两层筛选：

1. **主体层：**高流动性、高重要度的美国大公司，覆盖 Nasdaq 与 NYSE 及多个行业；
2. **验证层：**刻意加入拆股、分红、代码更名、同发行人多类别股票、ADR、交易所迁移和符号标点等真实数据边界。

SPX 和 Nasdaq-100（`NDX`）属于独立的指数 manifest，不计入这 30 只，也不以 SPY/QQQ 之类 ETF 偷换指数身份。Park 已确认 “Nasdaq” 采用 Nasdaq-100（`NDX`）。

## 2. 事实快照与判断边界

### 2.1 已核实的当前快照事实

- 2026-08-31（中国时间、美股常规时段前）从 Nasdaq 官方 [Stock Screener API](https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&offset=0&download=true) 取得证券名称、最新显示价格、成交量、市值和行业字段；并从每只证券的官方 `summary` endpoint 取得 Nasdaq 标记为 `Average Volume` 的数值。
- Nasdaq 官方 [market-movers endpoint](https://api.nasdaq.com/api/marketmovers?assetclass=stocks) 在 2026-08-31 04:07 ET 的 “Most Active by Dollar Volume” 快照中包含 `NVDA`、`MSFT`、`AMZN`、`AAPL` 和 `TSLA`，支持把它们作为高流动性核心；这仍然只是一时点事实，不是永久排名。
- Nasdaq Trader 在文件页脚标记为 `08/31/2026 03:02` 的 [Nasdaq-listed directory](https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt) 和 [other-exchange directory](https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt) 核实了本清单的当前主要上市地；字段定义见 [Symbol Directory Definitions](https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs)。
- Nasdaq endpoint 没有同时返回可供持久引用的行情 `as-of` 时间，官方页面也没有说明 `Average Volume` 的平均窗口。因此下表只把它作为 **2026-08-31 筛选时的流动性证据**，不把它写成“30 日均量”或长期保证。
- 30 只中最低的 Nasdaq-labelled Average Volume 仍约为 223 万股（COST），最高约为 1.324 亿股（NVDA）；价格快照从约 $13.88（F）到约 $1,174.61（LLY），足以同时测试高低价格和不同交易活跃度。

### 2.2 属于本报告的选择判断

- “大/重要”“适合 MVP”“验证价值高”是本报告的工程判断，不是交易所事实。
- 一日或一个接口窗口的流动性会变化，所以 basket 在 MVP 开始后不应按行情自动轮换。
- 纳入一只股票不表达看多、看空、预期收益或资产配置意见。

## 3. 精确 basket 与每只股票的验证职责

下表的价格和 `Avg vol` 均为 2026-08-31 调研时 Nasdaq 官方 summary endpoint 的显示值；`M` 为百万股。ticker 链接直接指向该证券的官方 Nasdaq JSON summary。

| # | Ticker | 主要上市地 | 价格快照 | Avg vol | 纳入理由与数据库验证职责 |
|---:|---|---|---:|---:|---|
| 1 | [NVDA](https://api.nasdaq.com/api/quote/NVDA/summary?assetclass=stocks) | Nasdaq | $217.55 | 132.39M | 超高流动性半导体主体；2024 年 10:1 拆股，用于检验拆股日前后 OHLCV、volume 与 adjustment factor。 |
| 2 | [TSLA](https://api.nasdaq.com/api/quote/TSLA/summary?assetclass=stocks) | Nasdaq | $348.75 | 39.50M | 高流动性汽车股；历史拆股和大波动/跳空用于检验极端 bar 与公司行动重算。 |
| 3 | [AAPL](https://api.nasdaq.com/api/quote/AAPL/summary?assetclass=stocks) | Nasdaq | $319.70 | 55.37M | 高流动性 mega-cap；同时覆盖历史拆股和规律现金分红。 |
| 4 | [MSFT](https://api.nasdaq.com/api/quote/MSFT/summary?assetclass=stocks) | Nasdaq | $513.53 | 38.30M | 高美元成交额的软件公司；作为稳定、高价、分红型科技基准。 |
| 5 | [AMZN](https://api.nasdaq.com/api/quote/AMZN/summary?assetclass=stocks) | Nasdaq | $266.43 | 49.70M | 高流动性消费/云计算主体；2022 年 20:1 拆股，适合验证历史回填是否混用复权口径。 |
| 6 | [GOOGL](https://api.nasdaq.com/api/quote/GOOGL/summary?assetclass=stocks) | Nasdaq | $346.59 | 30.63M | Alphabet Class A；专门验证同一发行人的 class-share identity。MVP **只收 GOOGL，不收 GOOG**。 |
| 7 | [META](https://api.nasdaq.com/api/quote/META/summary?assetclass=stocks) | Nasdaq | $578.02 | 18.02M | 大型平台公司；`FB → META` 是必须正确归并、不能切断历史的 ticker alias 案例。 |
| 8 | [AMD](https://api.nasdaq.com/api/quote/AMD/summary?assetclass=stocks) | Nasdaq | $465.58 | 26.43M | 高流动性半导体；与 NVDA/INTC 构成同业但价格和成交特征不同的并发采集样本。 |
| 9 | [AVGO](https://api.nasdaq.com/api/quote/AVGO/summary?assetclass=stocks) | Nasdaq | $368.79 | 22.11M | 高美元成交额半导体/基础设施软件公司；用于拆股和现金分红的交叉验证。 |
| 10 | [INTC](https://api.nasdaq.com/api/quote/INTC/summary?assetclass=stocks) | Nasdaq | $89.47 | 112.14M | 高 share-volume、相对低价的大型半导体；压测每周期新增 bar 与 volume 精度。 |
| 11 | [PLTR](https://api.nasdaq.com/api/quote/PLTR/summary?assetclass=stocks) | Nasdaq | $186.29 | 42.58M | 高流动性软件股；2024 年在 ticker 不变时从 NYSE 转 Nasdaq，验证 venue history 不可覆盖成单一静态值。 |
| 12 | [NFLX](https://api.nasdaq.com/api/quote/NFLX/summary?assetclass=stocks) | Nasdaq | $81.72 | 42.68M | 高流动性媒体股；2025 年 10:1 拆股是离 MVP 最近的明确公司行动样本。 |
| 13 | [CRM](https://api.nasdaq.com/api/quote/CRM/summary?assetclass=stocks) | NYSE | $256.00 | 7.22M | NYSE 大型软件公司；验证同一行业跨交易所的 calendar/session 和 symbol routing。 |
| 14 | [JPM](https://api.nasdaq.com/api/quote/JPM/summary?assetclass=stocks) | NYSE | $357.62 | 19.16M | 大型银行；补足金融行业和规律分红数据。 |
| 15 | [BAC](https://api.nasdaq.com/api/quote/BAC/summary?assetclass=stocks) | NYSE | $62.32 | 63.92M | 高 share-volume 银行；与高价 JPM 形成同业的价格/成交量对照。 |
| 16 | [V](https://api.nasdaq.com/api/quote/V/summary?assetclass=stocks) | NYSE | $381.60 | 7.96M | 大型支付网络；覆盖 NYSE 高价、较低 share-volume 但高美元成交额的证券。 |
| 17 | [WMT](https://api.nasdaq.com/api/quote/WMT/summary?assetclass=stocks) | Nasdaq | $103.09 | 24.48M | 高流动性零售；用于消费行业、拆股与分红链路。 |
| 18 | [COST](https://api.nasdaq.com/api/quote/COST/summary?assetclass=stocks) | Nasdaq | $945.47 | 2.23M | 接近千美元价格、较低 share-volume；检验价格精度及正常交易与“低活跃/缺失”不可混淆。 |
| 19 | [KO](https://api.nasdaq.com/api/quote/KO/summary?assetclass=stocks) | NYSE | $89.66 | 15.55M | 用户明确要求；长期拆股与季度现金分红使其成为 corporate-action reconciliation 基准。 |
| 20 | [PG](https://api.nasdaq.com/api/quote/PG/summary?assetclass=stocks) | NYSE | $143.78 | 6.57M | 大型日用消费公司；补足较稳定的现金分红型日常消费样本。 |
| 21 | [LLY](https://api.nasdaq.com/api/quote/LLY/summary?assetclass=stocks) | NYSE | $1,174.61 | 3.41M | **暂把用户说的“利来”解释为“礼来 / Eli Lilly”**；同时提供最高价和医药大型股样本，开始 freeze 前需口头确认这一解释。 |
| 22 | [JNJ](https://api.nasdaq.com/api/quote/JNJ/summary?assetclass=stocks) | NYSE | $268.04 | 6.43M | 大型医疗保健与规律现金分红样本；用于与 LLY 的同行业数据对照。 |
| 23 | [UNH](https://api.nasdaq.com/api/quote/UNH/summary?assetclass=stocks) | NYSE | $392.95 | 2.99M | 医疗保险服务，补足非制药医疗子行业及较低 share-volume 的高价股票。 |
| 24 | [XOM](https://api.nasdaq.com/api/quote/XOM/summary?assetclass=stocks) | NYSE | $156.71 | 24.58M | 高流动性能源公司；检验能源事件日的成交放大及现金分红。 |
| 25 | [CVX](https://api.nasdaq.com/api/quote/CVX/summary?assetclass=stocks) | NYSE | $201.86 | 9.83M | 第二只大型能源公司；用于同业数据一致性和 provider gap 对照。 |
| 26 | [F](https://api.nasdaq.com/api/quote/F/summary?assetclass=stocks) | NYSE | $13.88 | 70.55M | basket 中低价、高 share-volume 边界；与 TSLA 构成汽车行业价格尺度对照。 |
| 27 | [DIS](https://api.nasdaq.com/api/quote/DIS/summary?assetclass=stocks) | NYSE | $108.10 | 11.43M | 大型媒体/体验公司；与 NFLX 形成同主题但不同交易所和公司行动历史的对照。 |
| 28 | [CAT](https://api.nasdaq.com/api/quote/CAT/summary?assetclass=stocks) | NYSE | $800.25 | 3.26M | 高价工业公司；补足工业周期行业并测试低 share-volume 但高 notional 的正常数据。 |
| 29 | [BRK.B](https://api.nasdaq.com/api/quote/BRK%2FB/summary?assetclass=stocks) | NYSE | $505.00 | 5.84M | Class B 与点号 ticker；专门验证 `BRK.B / BRK/B / BRK-B` provider symbol normalization，不收 BRK.A。 |
| 30 | [TSM](https://api.nasdaq.com/api/quote/TSM/summary?assetclass=stocks) | NYSE | $417.52 | 10.48M | 唯一 ADR/ADS 样本；验证美国 K 线与境外发行人、ADR ratio、跨币种分红事件的身份边界。 |

## 4. 关键边界的一手证据

1. **Alphabet 只选一个 class。** Alphabet 的 SEC 文件明确列出 `GOOGL` 为 Class A、`GOOG` 为 Class C，均在 Nasdaq；Class B 不公开交易。[Alphabet filing](https://abc.xyz/assets/53/aa/c8121b9b5900f38838f4cfe6b7b6/fdab9de6a195d67ce5861b525627ac73.pdf) MVP 选择 `GOOGL`，不是因为它“更值得投资”，而是避免同一发行人被算成两只独立公司，同时仍保留 class-share 测试。
2. **META 历史不能从 2022 年重新开始。** Meta 向 SEC 披露其 Class A 股票于 2022-06-09 从 `FB` 改为 `META`，上市地与 CUSIP 不变。[Meta 8-K exhibit](https://www.sec.gov/Archives/edgar/data/1326801/000132680122000070/may312022-exhibit991.htm) 数据库应以稳定 `instrument_id` 串联 `FB → META`。
3. **PLTR 是 venue history 测试。** Palantir 的 SEC 8-K 记录其在 ticker 保持 `PLTR` 时，于 2024-11-26 从 NYSE 转至 Nasdaq。[Palantir 8-K](https://www.sec.gov/Archives/edgar/data/1321655/000132165524000219/pltr-20241114.htm) `primary_exchange` 不应只保存当前字符串并抹掉历史有效期。
4. **BRK.B 必须做 provider symbol map。** Berkshire 2025 Form 10-K 明确 NYSE symbols 为 `BRK.A` 与 `BRK.B`；Nasdaq API 却把路径/返回符号表示为 `BRK/B`。[Berkshire 10-K](https://www.sec.gov/Archives/edgar/data/1067983/000119312526083899/brka-20251231.htm) 建议 canonical display symbol 用 `BRK.B`，每个 provider 单独保存映射，禁止全局字符串替换。
5. **TSM 是 ADR，不是台湾普通股的同一 ticker。** TSMC 官方披露其 depositary receipts 在 NYSE 以 `TSM` 上市，且 1 ADR 对应 5 股普通股。[TSMC dividend page](https://investor.tsmc.com/english/dividends/1q26)、[TSMC financial statement](https://investor.tsmc.com/chinese/encrypt/files/encrypt_file/qr/phase4_reports/2026-04/5b6b7f218129782a5bf0366e7fd06aadc5c1515a/FS.pdf) 应保存 ADR identity/ratio，不能把 `TSM` 与台湾 `2330` 的 K 线直接拼接。
6. **LLY 是有意但暂定的语义解释。** Eli Lilly 2025 Form 10-K 确认 common stock ticker 为 `LLY`、上市地 NYSE。[Eli Lilly 10-K](https://www.sec.gov/Archives/edgar/data/59478/000005947826000013/lly-20251231.htm) 但“利来”本身可能是语音/输入误差，因此必须在 universe freeze 前让 Park 确认“利来 = 礼来”。
7. **拆股必须是真实验收用例。** NVIDIA 官方记录 2024 年 10:1 拆股，[NVIDIA IR](https://investor.nvidia.com/news/press-release-details/2024/NVIDIA-Announces-Financial-Results-for-First-Quarter-Fiscal-2025/default.aspx)；Tesla 官方记录 2022 年 3:1 拆股，[Tesla IR](https://ir.tesla.com/press-release/tesla-announces-three-one-stock-split)；Apple 官方列出 2020 年 4:1 拆股，[Apple IR FAQ](https://investor.apple.com/investor-relations/faq/default.aspx)；Amazon 官方列出 2022 年 20:1 拆股，[Amazon IR FAQ](https://ir.aboutamazon.com/faqs/default.aspx/1000/)；Netflix 的 SEC 文件记录 2025 年 10:1 拆股。[Netflix 8-K](https://www.sec.gov/Archives/edgar/data/1065280/000106528025000407/nflx-20251030.htm) 这些能验证 raw/adjusted 数据不能被混存。
8. **KO 是分红与拆股基准，而不只是用户点名。** Coca-Cola 官方说明股票在 NYSE 以 `KO` 交易、通常每年派息四次，并列出自 1919 年以来的拆股记录。[Coca-Cola shareholder FAQ](https://investors.coca-colacompany.com/shareowners/faqs)

## 5. 30 天 freeze 合同

建议将第一次端到端成功运行时的清单登记为 `us_mvp_v1`，并从该时刻起冻结 **连续 30 个自然日**：

- 30 天内不按成交量、市值、价格涨跌或指数调整自动增删；
- ticker 更名只新增 alias，不创建新的 instrument，不删除旧历史；
- 拆股、分红和 venue 变化更新事件/映射表，不改 basket membership；
- 停牌、缺数或退市不立即换股——保留成员与状态，让 MVP 真实检验 fail-closed、补洞和缺失语义；
- 唯一允许在启动前变更的是确认“利来”不是 Eli Lilly。若启动后发现 identity 错误，必须创建新 universe version 并记录原因，不能静默覆盖 `us_mvp_v1`；
- 第 30 天只基于工程验收决定是否扩容：coverage、freshness、重复 bar、gap、公司行动一致性、重跑幂等、备份与恢复。不要依据收益表现决定保留或删除。

## 6. 对数据库合同的最小要求

这个 basket 暴露出一个 ticker 字符串不够表达证券身份。至少应有：

| 字段 | MVP 要求 |
|---|---|
| `instrument_id` | 稳定内部 ID；不随 ticker 或 exchange 改变。 |
| `display_symbol` | 当前人类显示代码，如 `BRK.B`。 |
| `provider_symbol` + `source_id` | 每个数据源独立映射，如 provider 可能使用 `BRK/B` 或 `BRK-B`。 |
| `security_type` | `common_stock` 或 `adr`；本 basket 只有 TSM 为 ADR。 |
| `share_class` | 至少区分 GOOGL Class A、BRK.B Class B。 |
| `primary_exchange_valid_from/to` | 支持 PLTR 这类 venue history。 |
| `ticker_alias_valid_from/to` | 支持 `FB → META`，且历史查询仍归属同一 instrument。 |
| `corporate_action` | split、cash dividend、ADR ratio 等事件独立保存。 |
| `price_adjustment_basis` | raw 与 adjusted 必须显式，不能把两者塞进同一 series。 |
| `universe_version` | `us_mvp_v1` + effective dates，确保 30 天 freeze 可复验。 |

## 7. 仍需 Park 确认的一件事

**“利来”是否就是“礼来制药 / Eli Lilly (`LLY`)”？** Nasdaq identity 已确认：`NDX`。

除此之外，这 30 只可以直接作为后续 spec 的固定美股 MVP universe。它有足够流动性支持真实的 15m/4h/1d/1w 采集验证，也有足够复杂度暴露一个长期数据库必须解决的身份和公司行动问题，但仍远小于全美股。

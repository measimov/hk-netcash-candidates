# 筛选算法

## 投资画像

目标是贴近“管我财”式港股建仓思路：

- 不局限港股通，覆盖香港上市普通股。
- 偏好市值较小、流动性较差、被市场忽视的公司。
- 现金类资产覆盖负债，最好现金类资产大于总负债。
- 估值低，股息/回购能提供真实股东回报。
- 买入逻辑允许慢慢排队，但必须接受长期被套风险。

## 股票池初筛

剔除 ETF、ETN、基金、REIT、牛熊证、权证、债券、票据等非普通股。

初始行情/估值约束：

- 股价大于 0。
- 总市值约 `2e8` 到 `8e10` 港币。
- PB 在 `0.05` 到 `1.8`。
- PE TTM 或动态 PE 在 `0.5` 到 `25`，缺失可保留。
- 最新成交额在 `5e4` 到 `1.5e8` 港币。

初筛排序偏好：

```text
prefilter_rank =
  PB分位 * 0.35
  + 市值分位 * 0.30
  + 成交额分位 * 0.20
  + PE分位 * 0.15
```

分位越低越优先，体现“小、市净率低、低流动性、低估值”的偏好。

## 财务指标

主要使用 Tushare 港股三表：

- `hk_balancesheet`
- `hk_income`
- `hk_cashflow`

年报口径优先取 `12-31` 行；非 12 月财年公司退而取同年度最后一期，并在复核中标注需要人工核年报。

核心指标：

- `cash_like = 现金及等价物 + 短期存款/定期存款 + 短期投资/金融资产`
- `strict_cash_like = 现金及等价物 + 短期存款/定期存款`
- `interest_debt = 短债 + 长债 + 租赁负债`
- `net_cash_after_all_liab = cash_like - total_liabilities`
- `cash_to_liab = cash_like / total_liabilities`
- `net_cash_to_mv = net_cash_after_all_liab / market_cap`
- `profit_positive_years = 近4年股东应占利润为正年数`
- `cfo_positive_years = 近4年经营现金流为正年数`
- `cfo_to_profit_latest = 最近一年CFO / 股东应占利润`
- `oneoff_ratio_latest = 一次性/其他收益 / 股东应占利润绝对值`
- `shareholder_return = 已付股息 + 回购股份`
- `dividend_paid_yield_est = shareholder_return / market_cap`

## 财务硬门槛

主榜保留：

```text
cash_like > interest_debt
profit_latest > 0
profit_positive_years >= 2
latest_report_date >= 2024-12-31
```

然后优先排序：

```text
strict_net_cash = cash_like > total_liabilities
sort by strict_net_cash desc, score desc
```

## 主评分公式

```text
score =
  clamp(cash_to_liab, 0, 2.0) * 18
  + clamp(net_cash_to_mv, -1, 1.5) * 16
  + profit_positive_years * 5
  + cfo_positive_years * 5
  + clamp(cfo_to_profit_latest, 0, 2.0) * 8
  + clamp(dividend_paid_yield_est, 0, 0.12) * 300
  + clamp(profit_cagr_3y, -0.4, 0.4) * 20
  - clamp(oneoff_ratio_latest, 0, 1.5) * 12
```

如果现金流口径没有 `dividend_paid_yield_est`，用 DPS 估算收益率补充加分：

```text
clamp(dps_yield_est, 0, 0.12) * 220
```

## 二次检验

前 20 名二次检验不再按主评分排序，而是先给 `A/B/B-/C`：

- `A`: 暂未发现硬伤。
- `B`: 有 1-2 个观察项。
- `B-`: 有 3 个以上观察项。
- `C`: 有硬伤，例如近期 profit warning 或一次性/其他收益占利润超过 50%。

观察项包括：

- 最近派息/利润超过 120%。
- 4 年 CFO/利润均值低于 70%。
- 经营现金流不连续。
- 收入或利润趋势明显下滑。
- 主要靠回购，缺少稳定现金股息。
- 应收/资产偏高。
- 地产/物业链、金融/放贷属性需要折价。

## 治理风险过滤

治理覆盖层以公开信息为提示，不直接作事实定罪：

- HKEX 监管公告。
- SFC 执法新闻。
- 披露易标题模式。

风险关键词包括但不限于：

- 违规、调查、纪律处分、内幕消息。
- 大额配股、供股、可转债、反复摊薄。
- 停牌、延迟刊发、核数师辞任、内控问题。
- profit warning、重大亏损、清盘、诉讼。
- 私有化、要约、关连交易、小股东权益风险。

输出 `Clean / Watch / Amber / Red`，主榜之外另给治理过滤列表。

## LLM/DPSK 使用边界

LLM 只做汇总和研究提示，不参与确定性打分：

- 输入是已生成的 CSV 汇总 JSON。
- 输出是候选优先级、风险解释、复核清单。
- 不允许 LLM 编造数据源之外的新事实。
- 密钥读取环境变量 `DPSK_API_KEY`，不写入任何产物。

## 沪深上市红利ETF实时股息率

ETF 口径：

- 使用 Tushare `fund_basic(market='E')` 获取沪深场内基金。
- 仅保留已上市且名称含 `ETF` 的红利/股息/高股息相关基金。
- 纳入在沪深上市的恒生、港股通、H股相关红利/高股息 ETF。
- 排除 `联接`、`LOF`、`混合`，并剔除纳指、日经、美国、德国、全球等非本次范围主题。

实时股息率：

```text
div_cash_ttm = 近12个月每份现金分红合计
dividend_yield_ttm = div_cash_ttm / realtime_price
```

注意：Tushare `fund_div` 可能对同一 ETF 分红事件返回多行，计算前按 `event_date + div_cash` 去重。

实时价格：

- 优先使用新浪实时行情批量接口。
- NAV 使用 Tushare `fund_nav` 最近可得净值。
- 折溢价 = 实时价格 / 单位净值 - 1。

多维综合分：

```text
score =
  股息率分位 * 35
  + 实时成交额分位 * 20
  + 低费率分位 * 15
  + 折溢价质量分位 * 10
  + 近3年分红年数/3 * 10
  + 上市年限分位 * 10
```

其中折溢价质量使用 `-abs(premium_rate)`，即越接近 NAV 越好。

## 特殊高分红观察池

这个榜单用于捕捉主榜容易漏掉的“高现金分红但静态指标异常”港股，典型情形包括负 PE、最近完整财年亏损、非 12 月财年被中报/年报混淆、经营现金流短期波动。

初筛条件：

- 港股普通股，剔除 ETF、REIT、权证、债券等非股票标的。
- 市值约 2 亿至 200 亿港元，PB 0.05 至 1.3。
- 流动性参考值不低于 5 万港元，流动性参考值为 `max(实时成交额, Tushare近日日均成交额, Tushare近日中位成交额)`。

财务口径：

- 对每只股票先识别财年期末。若 Tushare 同时存在 `0630` 和 `1231` 等期末，选择收入规模最大的重复期末作为年度口径，避免把半年报当成年报。
- 复用主榜三表指标：现金类资产、总负债、有息负债、利润、经营现金流、已付股息、回购。

入池条件：

- 现金类资产高于有息负债，且现金/总负债不低于 0.8。
- 最近现金股东回报率不低于 6%。
- 近四个年度至少两年盈利、两年经营现金流为正。
- 最新报告期不早于 2024-06-30。

排序：

- 股东回报率 35%。
- 现金/负债与净现金/市值 30%。
- 盈利与经营现金流年数 16%。
- 低 PB 10%。
- 流动性 5%。
- 最近亏损、最近经营现金流为负、一次性收益占比高、非 12 月财年分别扣分。

该榜单是人工复核池，不等同主榜；进入后仍需单独检查分红持续性、亏损性质、资产减值、公允价值变动和治理风险。

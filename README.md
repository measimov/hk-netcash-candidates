# 港股净现金候选列表

这是一个静态导出版，也包含可复跑的流水线代码。结果覆盖港股净现金/股东回报筛选、前二十名二次检验、宽口径候选池、治理风险过滤层和可选 DPSK 汇总。

数据来源以 Tushare 财务数据为主，辅以腾讯/东方财富行情、HKEX/SFC 官方公开信息和披露易标题扫描。结果仅用于研究和复核，不构成投资建议。

## 运行

```bash
python -m pip install -r requirements.txt
export TUSHARE_TOKEN="..."
python -m hk_netcash_pipeline.cli --profile refresh
```

如需 DPSK/DeepSeek 汇总：

```bash
export DPSK_API_KEY="..."
python -m hk_netcash_pipeline.cli --profile refresh --use-llm
```

真实密钥只放在本地环境变量或未提交的 `.env`，不要写进代码或 CSV/HTML。

## 文档

- [架构与流程](docs/architecture.md)
- [筛选算法](docs/screening_algorithm.md)

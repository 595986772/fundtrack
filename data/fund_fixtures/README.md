# FundScope Fund Fixtures

这组 Fixture 保存 FundScope 第一阶段使用的固定基金样本：

- `110020`：宽基指数样本。
- `161725`：行业主题样本。
- `110007`：债券基金样本。

净值时间窗口固定为 `2023-08-25` 至 `2026-08-25`。数据用于开发、
测试和演示，不构成投资建议。

`fund_disclosures.json` 另外保存三只样例基金最新可用季报中的股票、债券和行业配置。
它不是实时持仓；页面会同时展示报告日期和新鲜度。

`fund_corporate_actions.json` 保存同一冻结区间内的现金分红和份额折算。风险计算先用
这些记录重建分红再投资总回报，避免把除息造成的单位净值下降误判为亏损或回撤。

手工刷新：

```bash
conda run --no-capture-output -n dl python scripts/freeze_fund_fixtures.py
```

刷新持仓披露：

```bash
conda run --no-capture-output -n dl python scripts/freeze_fund_disclosures.py
```

普通单元测试只读取已冻结的文件，不访问外部接口。

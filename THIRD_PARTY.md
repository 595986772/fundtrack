# 第三方组件与数据

- Web 图表使用 ECharts 6.0.0，许可证保留在 `app/static/vendor/ECHARTS-LICENSE.txt`。
- 小程序使用 ECharts for Weixin、Lucide、Morphdom，版本与许可证位置见 [小程序第三方说明](miniprogram/THIRD_PARTY.md)。
- Python / Node 依赖由各自包管理器安装，遵循各包许可证；本仓库不改变第三方组件的许可。
- `data/fund_fixtures`、`data/market_fixtures` 是注明日期与来源的冻结样例，仅用于复现开发与测试，不是实时数据服务，也不代表开发者实际资产。
- 基金季报属于原发布方。仓库仅保存来源、哈希和评测标注，不分发完整 PDF 与逐页全文；`scripts/prepare_report_fixtures.py` 按用户主动操作从原来源下载，下载后仍应遵守原发布方条件。

本次发布未为项目自有代码新增开源许可证；代码可公开查看不等于自动授予再分发或商用授权。

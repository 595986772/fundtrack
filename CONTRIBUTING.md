# 开发约定

使用 Python 3.12，运行方式见 README。新增数据库字段需提供 Alembic 迁移；不要通过删除本地数据库规避迁移问题。接口行为变化应补充相应测试，新增工具需要同时覆盖权限与错误返回。

提交前执行 `python -m scripts.check_release` 和 `node --test tests/frontend/*.test.cjs`。小程序改动还需执行 `pnpm test` 与 `pnpm build`。外部服务测试使用独立数据库与账号，不复用真实持仓。

提交信息说明实际变化，例如 `fix: keep realized profit after closing a position`。一个提交尽量处理一个问题，不混入运行数据、生成文件和与本次修改无关的格式调整。

保留第三方版权声明。复现问题时提供运行环境、最小步骤和已脱敏的报错，不提供完整 `.env` 或生产数据库。

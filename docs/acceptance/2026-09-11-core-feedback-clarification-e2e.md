# SemaRail Core 问题诊断、反馈与查询确认验收

日期：2026-09-11

状态：通过。交付范围按产品决策收敛为 SemaRail Core、MCP、Semantic Console
和 PostgreSQL；特定 Agent UI 插件不属于本仓库，待目标平台稳定后在独立插件
项目中实现。

## 最终制品

- 包：`@hejielijob/semarail-core@0.1.0-alpha.4`
- 文件：`dist/hejielijob-semarail-core-0.1.0-alpha.4.tgz`
- SHA-256：`3d2dcc558b84d042c10c9f9d56e58b91e66b8d247808dbf83d3d7dcef1203f85`
- 包内容验证：66 个受控文件（含前端依赖许可证）；不包含客户端适配、
  插件清单或测试文件。

## 范围与证据

| 能力 | 主要证据 |
| --- | --- |
| 结构化错误与统一 trace | Core RPC v2、Sidecar v2、MCP 详细错误测试 |
| 自动问题记录与独立诊断存储 | 失败自动捕获、脱敏、30 天清理、游标分页测试 |
| 重试关联 | 独立 trace 与 `original_query_id`、主体和项目隔离测试 |
| 反馈与幂等 | Core API、stdio MCP、Console API 和 UI 测试 |
| Console 管理与回归导出 | 筛选、分页、状态、重复关联和审核导出测试 |
| 查询确认 | metric、timeRange、granularity、businessDefinition 规则及失效测试 |
| PostgreSQL 权限 | 表/列/行策略、缺失属性、RLS、超时和取消门禁 |

## 本轮验证

- `pnpm lint`：通过。
- `pnpm typecheck`：通过。
- `pnpm build`：通过；工作区仅包含 Core、Contract 与 Console Web。
- `pnpm test`：通过，共 447 项（Core CLI 6、Contract 13、Console Web
  125、Sidecar 107、Semantic Console 196）。
- `pnpm acceptance:mcp`：通过；语义 MCP 和治理查询 MCP 使用真实协议客户端。
- `pnpm package:core`：通过；生成并校验上述 tarball 与哈希。
- 真实 PostgreSQL 数据门禁和 PostgreSQL 控制面门禁已在同一实现版本通过；
  它们仍分别由 `acceptance:postgres` 与 `acceptance:control-postgres` 保留。

## 移除项

已删除旧的 Host、Client、Bundle 和平台插件包，以及平台安装、会话回放、
拆包适配验收脚本和所有相关依赖。Core 的打包复制逻辑已收归
`packages/core/scripts/build.mjs`，因此独立制品不再依赖任何适配包源码。

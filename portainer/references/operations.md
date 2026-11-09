# API 操作

路径传给 `portainer.py request --instance ALIAS --path 'PATH'`。E、C、S 分别替换为实时环境、容器、Stack ID；查询参数用 当前运行环境的 URL 编码函数 编码，复杂 JSON 用 UTF-8 文件传 `--body-file`。

## 只读

| 目的 | GET 路径 |
| --- | --- |
| 版本、环境 | `/api/status`、`/api/endpoints` |
| Docker 版本、信息 | `/api/endpoints/E/docker/version`、`/api/endpoints/E/docker/info` |
| 全部容器 | `/api/endpoints/E/docker/containers/json?all=true` |
| 容器详情 | `/api/endpoints/E/docker/containers/C/json` |
| 日志 | `/api/endpoints/E/docker/containers/C/logs?stdout=true&stderr=true&tail=100&timestamps=true`，加 `--docker-logs` |
| 单次统计 | `/api/endpoints/E/docker/containers/C/stats?stream=false` |
| 镜像、网络、卷 | `/api/endpoints/E/docker/images/json`、`/api/endpoints/E/docker/networks`、`/api/endpoints/E/docker/volumes` |
| Stack 列表、定义 | `/api/stacks`、`/api/stacks/S/file` |

Stack 列表按 EndpointId 筛选。inspect、Stack 定义和日志可能含秘密，只展示相关且脱敏的内容。脚本缓冲响应，不用于无界 follow/stats/events 流、交互式 exec 或 WebSocket。

## 已授权变更

以下均需 `--allow-mutation`：

- 启动：`POST /api/endpoints/E/docker/containers/C/start`。
- 停止：`POST /api/endpoints/E/docker/containers/C/stop?t=30`。
- 重启：`POST /api/endpoints/E/docker/containers/C/restart?t=30`。
- 删除：`DELETE /api/endpoints/E/docker/containers/C`，不默认 force 或 v。删除卷和 prune 必须授权具体范围。
- 创建：`POST /api/endpoints/E/docker/containers/create?name=NAME` + Docker 创建 JSON，然后单独 start。根据需求和现有 inspect 保留挂载、网络、端口、变量、重启策略、用户和资源限制。
- 拉取镜像：`POST /api/endpoints/E/docker/images/create?fromImage=IMAGE&tag=TAG`。流式 JSON 中的 error 也表示失败，不能只看 HTTP 200。私有仓库认证需按版本另行实现，当前脚本不提供附加认证头参数。

重启不更新镜像。升级前检查 Compose/Stack 归属，由 Stack 管理的容器优先更新定义，避免配置漂移。

## Stack

先查 Type、EndpointId、GitConfig，保存 `/api/stacks/S/file` 和必要 Env。文件 Compose、Git Stack、Swarm 的接口及参数不同，先查匹配实际版本的 schema。不要用通用 PUT 覆盖 Git Stack，也不要跨实例复用 Stack ID。

文件型 Compose 的部分版本使用 `PUT /api/stacks/S?endpointId=E`，字段可能包含 `stackFileContent`、`env`、`prune`、`pullImage`。核实版本后保留变量，避免清空 Env；prune/pullImage 只在用户意图要求时启用。创建接口按版本及来源查询，不假设 `/api/stacks` 接受统一请求体。

超时后先查 Stack、容器状态和健康检查，确定是否生效再重试。回滚应检查数据/迁移兼容性，恢复 Compose 不等于可安全回滚数据。

## 官方资料

- [认证](https://docs.portainer.io/api/access)：X-API-Key。
- [Docker 代理示例](https://docs.portainer.io/api/examples)：`/api/endpoints/{id}/docker`。
- [版本 API 文档](https://docs.portainer.io/api/docs)：按实际 edition/version 查询。
- [Docker Engine API](https://docs.docker.com/engine/api/)：资源操作语义。

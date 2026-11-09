---
name: portainer
description: 通过用户指定的 Portainer 实例管理 Docker 环境、容器、镜像、网络、卷和 Stack。用户要求访问 Portainer 或明确由 Portainer 管理的部署时使用；普通本地 Docker 开发不默认使用。
---

# Portainer

实例、别名、说明和凭据均来自[本地配置](references/configuration.md)，不固定服务器、环境 ID、MCP 名称、执行工具或操作系统。

## 发现执行能力

检查当前智能体的工具目录和说明，按 Portainer、环境发现、容器、Stack 等能力查找可用连接器；通过聚合网关时先用该网关的发现机制读取真实工具签名。只使用已发现的名称和参数。匹配连接器必须针对用户指定的实例；不要把另一个 Docker 主机当作这个 Portainer 实例。

有合适连接器时使用其受控接口。否则发现可用 Python 3、本地命令执行和配置读取能力，按技能元数据中的实际目录定位 `scripts/portainer.py`，先读 `--help`。客户端仅依赖标准库，支持 Windows、Linux、macOS；本机已有 DPAPI 凭据时才需要发现可用 PowerShell 来解密。不要硬编码解释器或宿主工具路径。

若当前执行环境缺少配置或不能到达目标，说明缺口；不要将本地凭据传到另一个执行环境，或擅自转成 SSH/其他主机管理任务。

## 操作流程

1. 用连接器或脚本 `instances` 发现本机配置的实例，根据用户称呼、实例说明和默认配置选定目标。多个目标无法区分时再询问。
2. 只读查询 `/api/status` 和 `/api/endpoints`，确认版本、环境名称、类型与状态。实例和 environment 是两级对象，始终带实例上下文；环境 ID 必须从实时结果获取。
3. API 路径通过 `/api/endpoints/{id}/docker/...` 代理 Docker Engine；资源 ID 从列表或 inspect 获取，Docker API 版本从 `/version` 获取。操作见[API 参考](references/operations.md)。
4. 在用户授权范围内执行。重启已明确指定的容器无需重复确认；删除持久卷、清理数据、部署或影响范围不明的操作须先明确范围。exec、Stack 更新和创建特权容器属于变更。
5. 修改前保存必要的 inspect 或 Stack 定义到任务 `work/`，其中可能含秘密；不要提交或原样展示。变更后用独立 GET 检查状态、健康和相关日志，区分请求成功与应用可用。
6. 超时后先读状态再决定是否重试。401/403 核对授权，404 核对版本和目标；网络不可达不代表服务器停机。

## 脚本入口

在已发现的 Python 3 后传入脚本实际路径和参数，例如：

```text
scripts/portainer.py instances
scripts/portainer.py request --instance ALIAS --path /api/status
scripts/portainer.py request --instance ALIAS --path /api/endpoints
```

`ALIAS` 必须来自实例配置；参数在子命令之后。其他选项包括 `--config`、`--body-file`（UTF-8 JSON）、`--out-file`（原始响应）和 `--docker-logs`（解码日志帧）。

客户端拒绝重定向、不自动重试，默认绕过系统代理；确需代理时使用 `--use-proxy`。写请求必须加 `--allow-mutation`，此开关不代替用户授权。脚本不处理无限日志流、交互式 exec 或 WebSocket；需要这些能力时发现适合的工具并核对实际目标。认证头不可写进命令或展示给用户。

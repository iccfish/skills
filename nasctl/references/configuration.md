# 本地配置

按顺序解析：子命令 `--config FILE`、`NASCTL_CONFIG`、`AGENT_SKILLS_CONFIG_DIR/nasctl.json`、`XDG_CONFIG_HOME/agent-skills/nasctl.json`，最后是用户目录下 `.config/agent-skills/nasctl.json`。

创建于技能仓库外的 JSON 示例：

```json
{
  "base_url": "http://nasctl.example.local:3001",
  "openapi_path": "/api-docs/openapi.json",
  "timeout_seconds": 30,
  "use_proxy": false
}
```

`base_url` 可含部署前缀，例如 `https://example.com/nas`，不能带用户名、密码、查询或 fragment。可用 `NASCTL_BASE` 临时覆盖无认证的地址；配置了认证时更换目标必须使用独立配置，防止把原凭据发送到其他站点。默认绕过系统代理，配置 `use_proxy:true` 才使用代理；不关闭 TLS 验证。

认证要求以目标部署、反向代理配置和当前 OpenAPI 为准；不要根据其他实例的行为推断无需认证，也不应编造 NasCtl 登录或 Token 接口。

若反向代理要求认证，可按其实际契约在本地配置引用环境变量：

```json
{
  "base_url": "https://nasctl.example.com",
  "headers_env": {
    "Authorization": "NASCTL_AUTHORIZATION"
  }
}
```

`NASCTL_AUTHORIZATION` 存放该部署要求的完整头值；不要假设一定是 Bearer。也支持本地 `headers` 对象，`headers_env` 中同名值覆盖它；变量缺失即报错。配置文件仅允许当前用户读取，不提交仓库、日志或交付包。不得用命令参数或聊天传实际秘密。连接器已管理认证时使用其配置，不复制凭据。

脚本无自动重试、禁止重定向、响应上限 16 MiB、种子解析上传上限 10 MiB。超时可在 1–600 秒范围内设置；下载器本身的 `timeoutMs` 是另一个配置，不能混用。请求 JSON 优先写为当前任务 `work/` 下的 UTF-8 文件，`--query NAME=VALUE` 负责 URL 编码，可重复使用；上传 `.torrent` 用 `--upload-file`，不可与 `--body-file` 同用。

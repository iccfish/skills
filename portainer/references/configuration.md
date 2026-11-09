# 本地配置

配置与凭据放在技能仓库外，路径按顺序选择：

1. 子命令 `--config FILE`。
2. 环境变量 `PORTAINER_CONFIG`。
3. `AGENT_SKILLS_CONFIG_DIR/portainer.json`。
4. `XDG_CONFIG_HOME/agent-skills/portainer.json`。
5. 当前系统用户目录下 `.config/agent-skills/portainer.json`。

示例仅含占位地址与环境变量名称：

```json
{
  "default_instance": "lab",
  "instances": {
    "lab": {
      "base_url": "https://containers.example.com",
      "description": "实验环境",
      "api_key_env": "LAB_PORTAINER_API_KEY"
    }
  }
}
```

在实际执行进程的环境中设置 `LAB_PORTAINER_API_KEY`，不要在命令参数或聊天中传真实值。也支持在仓库外 JSON 中以 `api_key` 保存实际值；该文件须限制为当前用户可读，不放进 Git、云盘、交付物或任务输出。配置 `api_key_env` 后变量未设置即报错，不回退至其他凭据。

迁移已有 Windows 配置时可使用 `api_key_dpapi` 保留加密值。客户端会主动查找 PowerShell，仅限原 Windows 用户/机器解密。其他机器重新配置自己的环境变量或本地密钥，不同步 DPAPI 值。可分发的技能不包含任何 DPAPI 值。

`PORTAINER_API_KEY` 可临时覆盖选定实例的凭据；它会用于本次选定实例，执行者须核对目标，不能将同一覆盖变量无差别用于多个实例。API 请求使用 `X-API-Key`。

实例别名和说明完全由本地配置决定。仅一个实例时可省略 `--instance`；多个实例时使用 `--instance` 或本机 `default_instance`。`instances` 仅列出别名、地址和说明，不返回密钥。端点、容器和 Stack ID 从实际服务发现，不放进通用技能。

`base_url` 必须是 HTTP(S) origin，不接受内嵌凭据、路径、查询参数或 fragment。默认绕过系统代理；需要时用 `--use-proxy`。不关闭 TLS 证书验证，不跟随重定向，也不自动重试写入。

# 本地配置

配置与凭据放在技能仓库外。路径按下列顺序选择：

1. 子命令的 `--config FILE`。
2. 环境变量 `MRDOC_CONFIG`。
3. `AGENT_SKILLS_CONFIG_DIR/mrdoc.json`。
4. `XDG_CONFIG_HOME/agent-skills/mrdoc.json`。
5. 当前系统用户目录下 `.config/agent-skills/mrdoc.json`。

技能不会自动生成真实凭据，不读取其他智能体的配置。用当前环境的文件工具在本机创建 JSON；限制文件为当前用户可读，避免放进云盘、Git、交付包或任务输出。

推荐用环境变量保存秘密，在本地 JSON 中引用变量名：

```json
{
  "base_url": "https://notes.example.com",
  "token_env": "MY_MRDOC_TOKEN",
  "default_project_name": "AI Notes"
}
```

把实际 Token 注入本机 `MY_MRDOC_TOKEN`；不要把真实值写进聊天或命令参数。也支持在该仓库外 JSON 中使用 `token` 存放实际值。配置了 `token_env` 时，变量未设置会报错，不静默使用另一凭据。临时环境变量 `MRDOC_TOKEN` 优先于两者。

站点优先级为 `--base`、`MRDOC_BASE`、`base_url`。更换站点不能复用原配置的 Token；用独立 `--config`，或同时设置新站点的 `MRDOC_TOKEN`。只接受 HTTP(S) origin，不接受 URL 内密码、路径、查询参数或 fragment。连接器自行管理认证时，按连接器配置使用，不复制秘密。

默认文集优先按 `default_project_name` 精确匹配实时列表；也可仅配置 `default_project_id`，仍需验证其出现在实时列表中。名称重复、目标缺失或未配置时停止创建并明确选择目标。显式 `create --pid ID` 覆盖默认设置，不修改本地配置。默认父节点为顶层；用户指定层级时使用 `--parent ID`。

迁移后的本机配置会保留原默认文集；这些设置不随技能分发。使用环境变量启动新进程时，应确保实际运行智能体/脚本的进程能继承该变量。

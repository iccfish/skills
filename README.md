# Skills

可在不同智能体和机器上安装的个人技能。技能源码不包含实际站点、API Key、Token 或本机配置。

| 技能 | 用途 | 本地配置 |
| --- | --- | --- |
| [mrdoc](mrdoc/SKILL.md) | 搜索、阅读和维护 MrDoc 私人笔记本 | [配置说明](mrdoc/references/configuration.md) |
| [portainer](portainer/SKILL.md) | 管理 Portainer 环境、容器与 Stack | [配置说明](portainer/references/configuration.md) |
| [NasCtl](nasctl/SKILL.md) | 检索 NAS 文件、媒体与种子，管理下载、同步与转码队列 | [配置说明](nasctl/references/configuration.md) |
| [meeting-transcribe](meeting-transcribe/SKILL.md) | 按句校准归属、试听确认声纹，最终生成含待办与文件入口的单页 HTML 会议纪要 | [配置说明](meeting-transcribe/references/configuration.md) |
| [everything](everything/SKILL.md) | Windows 下通过 HTTP 或本机 ES 检索文件信息，管理并发现多个实例 | [配置说明](everything/references/configuration.md) |

## 安装

使用支持通用 Git 源的 `skills` CLI；本仓库当前为私有仓库，安装前先配置本机 GitHub 凭据：

```powershell
npx skills add https://github.com/iccfish/skills.git --list
npx skills add https://github.com/iccfish/skills.git --skill mrdoc portainer -g -a codex
npx skills add https://github.com/iccfish/skills.git --skill nasctl -g -a codex
npx skills add https://github.com/iccfish/skills.git --skill meeting-transcribe -g -a codex
npx skills add https://github.com/iccfish/skills.git --skill everything -g -a codex
```

其他智能体可更换 `-a` 参数；没有对应安装适配器时，将单个技能目录复制到该智能体支持的技能目录。技能使用时会发现可用工具与运行环境，不绑定固定 MCP 名称或解释器路径。

## 本机设置

按照各技能配置说明，在技能仓库外创建 `mrdoc.json`、`portainer.json`、`nasctl.json`、`meeting-transcribe.json` 和 `everything.json`。默认位置是用户目录下的 `.config/agent-skills/`，也支持 `AGENT_SKILLS_CONFIG_DIR`、`XDG_CONFIG_HOME`、技能专用环境变量和 `--config`。会议名单、声纹库和采样统一在 `.config/agent-skills/meeting-transcribe/`（可覆盖），旧数据用 `meeting-transcribe/scripts/migrate.py --apply` 备份迁移；模型缓存独立于源码目录；旧声纹库在更换前处理后需保留原库并从音频重新录入。

凭据可由配置引用环境变量，也可存放在权限受限的本地 JSON 中。Portainer 的可选 DPAPI 值只能由原 Windows 用户和机器解密，更换机器后应重新配置凭据。不要将真实配置、备份、认证头或带 Token 的 URL 提交到仓库。

## 更新

```powershell
npx skills check
npx skills update -g
```

已安装技能与本机配置分开管理，更新技能源码不替换机器上的凭据。

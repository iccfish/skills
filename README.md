# Skills

可在不同智能体和机器上安装的个人技能。服务地址、API Key、Token 和个人数据均通过仓库外配置提供；公开前的数据审计范围与历史清理状态见[发布审计](docs/publication-audit.md)。

| 技能 | 详细能力与交付 | 运行环境与依赖 | 本地配置 |
| --- | --- | --- | --- |
| [mrdoc](mrdoc/SKILL.md) | **文集与检索**：列出文集、分页读取目录和文档列表，跨本人文档或限定文集搜索标题/正文，获取原始 Markdown。<br>**维护与交付**：新建、整篇更新、追加、改标题、移动层级、删除文档，上传图片和附件，返回文档链接；修改前读取原文并保留标题、所属文集及未修改字段。<br>**边界**：脚本只改写 Markdown 文档；富文本、在线表格及附件能力受服务端版本和权限限制。 | 可用的 MrDoc 专用连接器，或 Python 3 标准库客户端；需要自有 MrDoc 站点和 Token。 | [配置说明](mrdoc/references/configuration.md) |
| [portainer](portainer/SKILL.md) | **实例与诊断**：管理目标选择，发现环境与资源 ID，查询容器详情、健康、日志、单次统计及镜像、网络、卷、Stack 定义。<br>**授权运维**：通过 Portainer 代理执行容器创建、启动、停止、重启、删除和镜像拉取；按实际版本处理 Compose/Stack 变更，保存必要定义并复查运行状态。<br>**边界**：写请求需显式开关；脚本不提供交互式 exec、WebSocket 或无限日志流，私有镜像额外认证需另行适配。 | 可用的 Portainer 连接器，或 Python 3 标准库客户端；需要 Portainer 实例和 API Key。旧 DPAPI 凭据仅能在原 Windows 用户/机器解密。 | [配置说明](portainer/references/configuration.md) |
| [NasCtl](nasctl/SKILL.md) | **内容检索**：分别查询内容文件索引、磁盘目录条目、媒体、种子、MT 记录和转码历史，按哈希追溯位置与关联来源，处理分页和不同检索语义。<br>**下载与同步**：发现下载站及能力，解析/添加种子，控制任务、文件选择和优先级、移动目录、移除任务；查询空间并预演/执行 MT 标签及挂载空间同步。<br>**系统与结果**：查看系统、数据库、转码及队列状态，授权移除/清空队列；依据实时 OpenAPI 调用，区分部分失败、异步受理和实际完成，不提供启动转码接口。 | 可用的 NasCtl 连接器，或 Python 3 标准库客户端；需要兼容的 NasCtl 服务与实时 OpenAPI，认证依实际部署配置。 | [配置说明](nasctl/references/configuration.md) |
| [meeting-transcribe](meeting-transcribe/SKILL.md) | **转写与校准**：上传指定音视频到 OSS，调用百炼异步转写，保存原始 JSON/逐字稿；管理自定义热词及权重（新增默认 1），支持增删改查并自动随新任务提交；可选本地声纹暂定说话人，按句索引修正姓名、插话归属和文字，调整可累积重放。<br>**试听与声纹**：明确继续后生成分离稿，为每人提取两段不重叠 MP3；逐人试听确认后才入库，不足不凑数，拒绝时删除对应待确认样本。<br>**最终交付**：生成带摘要、议程、待办、原话索引和文件入口的离线 HTML 纪要；支持任务恢复、本地结果复用和旧数据迁移，避免纠错时重复付费转写。 | Python 3、ffmpeg/ffprobe、阿里云 OSS 与百炼凭据；声纹另需 NumPy、ONNX Runtime、kaldi-native-fbank。模型不随包分发，启用时按需下载至用户缓存。 | [配置说明](meeting-transcribe/references/configuration.md) |
| [everything](everything/SKILL.md) | **多实例管理**：列出、添加、更新、删除及选择默认实例；配置为空时发现本机 INI、插件与运行进程，验证 HTTP/ES 后登记，凭据保存在仓库外。<br>**文件信息检索**：按名称/路径、扩展名、正则、大小和时间等表达式查询，支持匹配模式、分页、排序、UTF-8 查询文件与 HTTP JSON/ES CSV 结果。<br>**边界**：HTTP 可连接本机或远程，ES 仅经本机 IPC 查询；返回索引信息，不默认读取、下载、打开或执行文件，不代为安装软件或修改索引服务。 | 仅 Windows，Python 3.10+ 标准库；需要可用的 Everything HTTP 接口，或运行中的 Everything 1.4+ 与 ES 1.1.0.37+。 | [配置说明](everything/references/configuration.md) |

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

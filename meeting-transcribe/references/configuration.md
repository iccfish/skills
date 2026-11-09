# 配置与本地数据

凭据不进入技能源码、模型目录或任务记录。配置模板为 `config.example.json`。

脚本需要 Python 3.10 或更新版本；本轮验证使用 3.12。声纹可选依赖还需支持对应平台/解释器，缺失或加载失败时应明确降级。

## 配置优先级

按字段：命令行覆盖项 > 显式 `--config`（或 `MEETING_TRANSCRIBE_CONFIG`）> 环境变量 > 用户配置 > 内置默认值。

用户配置默认 `~/.config/agent-skills/meeting-transcribe.json`，Windows 对应 `%USERPROFILE%\.config\agent-skills\meeting-transcribe.json`。这是本仓库跨平台约定；Windows 本身并没有统一规定必须使用 `.config`。支持 `AGENT_SKILLS_CONFIG_DIR` 或 `XDG_CONFIG_HOME/agent-skills`。

不再自动读取或写回会议目录的项目配置、技能目录 `config.json` 或旧 WorkBuddy 配置。需要临时配置时显式使用 `--config`；不存在或损坏时失败，不静默回退。相对路径字段以配置文件所在目录解析。检测到未迁移的旧技能配置/个人库时，提示先迁移，避免误用空库。

| 字段 | 环境变量 | 用途 |
|---|---|---|
| api_key | BAILIAN_API_KEY / DASHSCOPE_API_KEY | 百炼鉴权，左侧优先 |
| model | BAILIAN_ASR_MODEL | 默认 qwen-audio-3.1-asr-flash-filetrans |
| oss_endpoint | OSS_ENDPOINT | OSS 区域域名 |
| oss_access_key_id | OSS_ACCESS_KEY_ID | OSS 访问身份 |
| oss_access_key_secret | OSS_ACCESS_KEY_SECRET | OSS 密钥 |
| oss_security_token | OSS_SECURITY_TOKEN | 可选 STS 临时凭据 |
| oss_bucket | OSS_BUCKET | 上传 Bucket |
| oss_host | OSS_HOST | public 模式可选访问根域名 |
| oss_url_mode | OSS_URL_MODE | public 或 signed |
| ffmpeg_dir | FFMPEG_DIR | 同时包含 ffmpeg 与 ffprobe 的目录 |

`dashscope_base`、轮询间隔、等待时间、名单/声纹路径等使用 JSON 配置。不要打印整个配置对象。

## 热词配置

`hotwords` 为“热词到整数权重”的对象，默认空词表。通过 `--hotword-add` 新增时省略权重即为 1，可用 `--hotword-weight` 指定；支持列出、查询、改词、改权重和删除。词表保存在同一仓库外主配置，非空时自动随新转写请求的 `parameters.vocabulary` 提交。完整示例、保存优先级和恢复边界见[热词与权重](hotwords.md)。

## OSS 读取模式

- `public` 保持原行为，需要匿名可访问的对象 URL；不自动更改 Bucket ACL。
- `signed` 使用 OSS 源站的限时 GET URL，支持私有 Bucket，不使用 CDN 自定义域名；有效期为 `oss_url_ttl` 秒，默认 86400，配置范围 60–604800。实际提交前还要求覆盖 `max_wait + 300` 秒；额外预留排队和恢复时间，URL 到期后已提交任务不保证还能读取音频。
- 签名 URL 用 Range GET 检查，避免 GET 签名误用于 HEAD。日志和任务记录不保存该 URL。
- 仅删除本次 UUID 对象；清理失败会保留记录，绝不更改 Bucket 权限。自定义域名/CDN 的缓存可能独立于源站删除，需自行配置适合私人录音的缓存策略。

## 配置、数据和缓存分离

| 内容 | 默认位置或覆盖方式 |
|---|---|
| 名单 speakers.json、声纹库 voiceprints.json | 用户配置文件目录下的 `meeting-transcribe/`，即默认 `~/.config/agent-skills/meeting-transcribe/`；支持显式 `MEETING_TRANSCRIBE_DATA_DIR` |
| 模型/工具缓存 | `MEETING_TRANSCRIBE_CACHE_DIR`，否则 XDG_CACHE_HOME 下；Windows 默认 LOCALAPPDATA/Cache 下，其他平台默认 ~/.cache 下 |
| 单次会议产物 | 原媒体旁的整组 .asr.json/.meta.json/.txt/.task.json |
| 展示稿与首次归属 | 原媒体旁的 -原始转写稿.txt、-归属转写稿未确认版.txt、-归属校准版.txt、-说话人分离.txt；.attribution.json 为不可覆盖的首次归属快照 |
| 声纹采样及决定 | 声纹库同级的 声纹采样/姓名目录/ 两段 MP3；会议旁 .samples.json |
| 最终会议纪要 | 原媒体旁的 -会议纪要.html、.minutes.json（智能体整理内容）、.report.json（来源与输出版本）；无新增密钥或外部服务 |

`roster_path`、`vp_db`、`vp_model_path` 或命令行路径可显式覆盖。默认数据目录跟随 `AGENT_SKILLS_CONFIG_DIR`/`XDG_CONFIG_HOME`；不再用 XDG_DATA_HOME 或 LOCALAPPDATA 保存新的个人库。模型和工具是可重建缓存，不属于配置；单次会议记录继续随原媒体保存。

写入使用同目录临时文件原子替换。名单和声纹库检测读取后的版本变化，发现并发修改时拒绝覆盖，重新读取后再操作。`.lock` 标识同一文件正在处理；进程被强杀可能残留锁，确认没有相关进程后再手动移除，脚本不猜测并删除它。

不要提交真实配置、录音、转写产物、任务记录或个人库。文件默认使用当前用户目录；在共享机器上另行设置该目录的访问权限。

`vp_auto_enroll` 为旧字段，现已忽略；即使旧配置为 true，也只有用户试听后明确准确的采样才入库。先保存付费转写结果，再在首次归属或批准入库时处理声纹开关；待决用本地 JSON 继续，不重新上传转写。已有快照后的校准和继续不重复匹配。`vp_segments` 控制匹配建议提取，试听固定每人两段；两者均使用 `vp_min_seconds`/`vp_segment_seconds` 时长约束。

## 迁移已有配置与个人数据

先结束其他正在运行的会议处理脚本，再用技能脚本的绝对路径调用：

```powershell
python scripts/migrate.py
python scripts/migrate.py --apply
```

默认先检查而不写入。来源包含技能目录的真实 `config.json`、`speakers.json`、`voiceprints*.json`、旧用户数据目录和 Codex Windows 应用虚拟化目录中的个人库及 `声纹采样/`。旧 WorkBuddy 配置也可发现；发现多个配置时要求用 `--source-config` 选择，避免覆盖凭据。`--source-data` 可重复补充旧目录，`--meeting-dir` 可重复指定含 `.samples.json` 的会议目录；另从声纹记录的原始媒体引用发现会议目录。其他目录的清单需显式补充。

脚本将来源原件及摘要清单备份到新数据目录 `migration-backups/<批次>/`，校验目标后才移除旧文件；目标存在不同内容、来源变化、使用中锁或采样摘要异常均停止。原录音、转写稿、模型不移动。声纹采样清单和库内采样引用同步更新，采样身份、审核版本、试听决定、音频字节及向量保持不变，不重新调用 ASR、不重新入库。中断时保留备份与尚未删除的原件；依据备份 `index.json` 核对后恢复，勿盲目覆盖并发更新。

schema 1 旧库单独保存为 `voiceprints-legacy-v1.json`，不能直接用于新前处理。默认优先选取已存在的 `voiceprints-kaldi-v2.json` 为活动库；没有时使用新的 `voiceprints.json`，不混合旧向量。显式配置的自定义库路径保持选择，仅迁移本次发现的文件引用。完成后再次运行迁移检查应显示无需迁移；用 `transcribe.py --check-env` 检查实际配置加载。

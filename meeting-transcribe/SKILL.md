---
name: meeting-transcribe
description: 将会议或访谈音视频转为可审核逐字稿，先暂定归属再按句校准；用户明确继续后采样，逐人试听确认入库，最终生成带来源索引与产物链接的单页 HTML 会议纪要。
---

# 会议录音转写与人工审核

交付可校准转写稿，并在最终确认流程结束后生成单页 HTML 会议纪要。原始 ASR JSON 与原始转写稿不改写；人工操作保存在 meta 中并重放。发现可用终端、Python、ffmpeg/ffprobe，不绑定 MCP 工具名或解释器路径。

## 准备

首次使用读 [配置说明](references/configuration.md)。凭据放本地配置或环境变量，名单、声纹与采样默认放 `~/.config/agent-skills/meeting-transcribe/`，主配置为同级 `meeting-transcribe.json`。旧配置和个人库先用 `python scripts/migrate.py` 检查，再加 `--apply` 备份迁移。用脚本绝对路径调用，工作目录保持会议目录。先运行 `python scripts/transcribe.py --check-env`，自检不联网、不安装。ffmpeg 与 ffprobe 作为常规工具链始终检查。

用户请求转写指定文件授权该录音的上传及百炼调用；不额外上传无关文件。后续审核、继续处理、试听采样及入库都使用本地结果，不重新付费转写。

## 必须分阶段执行

1. **保存原始稿后立即暂定归属。** 运行 `python scripts/transcribe.py "会议.m4a"`。先保存原始 JSON 和仅含编号的 `会议-原始转写稿.txt`，随即执行一次声纹对照，阈值命中暂用候选姓名，未命中保留编号。冻结 `.attribution.json` 和 `会议-归属转写稿未确认版.txt`，每句显示时间戳及零起始原始句索引。向用户交付带姓名的稿件；未解决的编号，用带时间戳、句索引的原话询问主要身份，必须提示：**如出现错误归属情况，按最大匹配输入，后续可手动调整**。不把低分最高候选擅自当作身份，也不录入声纹。
2. **自然语言校准。** 主要身份确认用 `--speaker-map`；单句/插话纠错用 `--attribute`，逐字纠错用 `--replace-text`。用户可直接指定“句12是李四”；必要时 `--find "原话"` 核对定位，有歧义先询问。生成独立的 `会议-归属校准版.txt`，后续修改累积重放已有调整。原始 JSON、原始 TXT、归属未确认版和首次归属快照保持不变；不在校准时重新匹配。不能把一个插话的确认扩大为整个上游编号的姓名。详情见 [人工纠错](references/edits.md)。
3. **明确继续才分离及采样。** 只有用户说“继续处理”等明确指令后运行 `--from-json "会议.asr.json" --continue`。使用冻结的首次归属加累计校准记录：有校准用校准，否则用未确认版对应状态，不解析 TXT 推断时间。不重新匹配覆盖姓名；生成 `会议-说话人分离.txt`，不自动入库。
4. **交付两段样本。** 继续处理同时按当前归属和提取规则，为每个最终姓名（未命名者按编号）提取两段不重叠 MP3，放在声纹库旁 `声纹采样/<说话人目录>/`，文件名含姓名、源时间戳、批次和索引。采样清单 `.samples.json` 保存组 ID、原音频时间、状态和文件摘要。不足两段时明确说明不足并跳过，不复制或凑数。
5. **逐人试听确认。** 输出分离稿及采样后，使用智能体询问工具，逐人为两段音频提供可回听的文件/音频预览，问这两段是否都属于该人且没有混入他人。选项严格为“准确”和“不对/不确定”，不要默认选择准确；未提交的预选项不是确认。未知姓名另问姓名，先补映射并重新采样再确认。用户的编号姓名确认本身不算样本确认；历史建议也不算确认。
6. **按回答入库或删除。** 准确：`--from-json ... --samples-accept 组ID`，对两份已试听 MP3 分别计算并保存声纹；两段计算均成功才保存库。否则：`--samples-reject 组ID`，删除该组待确认 MP3 并跳过入库，保留转写稿及清单决定。可多次指定参数逐人处理，未回复者保持 pending。详见 [声纹与采样管理](references/voiceprint.md)。
7. **生成最终会议纪要。** 所有采样已接受、拒绝或明确不足后，智能体完整阅读最终校准/分离稿，按 [报告结构与生成规范](references/report.md) 整理带句索引的正文，调用 `scripts/report.py` 生成 `会议-会议纪要.html`。内置基本信息、摘要、实际议程、待办、生成物快捷链接与折叠原话；区分结论与建议，不编造日期、负责人或期限。正文和渲染绑定当前版本，后续改稿需重新整理。检查浏览效果及链接后交付 HTML；不再次要求用户发起生成纪要指令。

```bash
python scripts/transcribe.py --from-json "会议.asr.json" --replace-text "5=原文=>新文"
python scripts/transcribe.py --from-json "会议.asr.json" --attribute "8=插话起点..插话终点=李四"
python scripts/transcribe.py --from-json "会议.asr.json" --continue
python scripts/transcribe.py --from-json "会议.asr.json" --samples-list
python scripts/transcribe.py --from-json "会议.asr.json" --samples-accept 0 --samples-reject 1
```

TXT 是展示稿，审核的可重放依据是原始 JSON 加 meta 调整记录；不要直接改 TXT 后假定已同步到时间戳和采样。审核或姓名有变化时，先重新 `--continue`、试听新样本，再确认；不能沿用旧批准。

## 工具、恢复及返回

- 云端上传、任务编号、原始结果保存、OSS 清理顺序及恢复规则见 [任务与工具链](references/operations.md)。轮询失败用 `--resume`，不重新提交。恢复完成仍停在审核阶段。
- 首次归属及确认入库时处理声纹开关：false 关闭匹配，true 准备依赖/模型，null 条件齐备自动启用、否则退出 3 并询问。原始结果已保存，待决后用 --from-json 继续，不重新转写。只有用户选启用后才安装缺失依赖。关闭匹配仍可生成分离稿和试听 MP3，入库需要启用声纹。
- 返回各稿件位置、审核/继续阶段、人工调整 skipped/warnings/conflicts、采样 pending/accepted/rejected/insufficient 状态，以及云任务与清理状态。未确认者不宣称完成入库。
- 退出码 0 完成本次阶段，1 自检缺关键项，2 输入/配置/处理失败，3 声纹待用户选择，4 多文件部分失败。0 不代表样本已批准。
- [验证记录](references/verification.md) 区分旧流程真实测试、新流程离线及合成音频验证，不把测试通过当作真实识别准确率。

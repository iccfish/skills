# 本地声纹与试听采样管理

声纹匹配只给出身份建议。上游编号可能混入其他人的发言，确认编号姓名不证明其中每段音频纯净。主链路已移除自动入库；旧配置 `vp_auto_enroll=true` 也不能恢复自动入库。

## 首次归属与校准

原始结果保存后立即匹配一次，阈值命中只作暂定姓名；低分或无匹配保留编号，由智能体结合原话询问主要身份，并提示“如出现错误归属情况，按最大匹配输入，后续可手动调整”。首次姓名及候选冻结在 `.attribution.json`，未确认稿含时间戳和句索引。自然语言修改生成独立归属校准版，保留首次快照；校准及继续都不重新匹配。归属姓名确认不等于样本批准，不能用于直接入库。

## 继续与采样

```bash
python scripts/transcribe.py --from-json "会议.asr.json" --continue
python scripts/transcribe.py --from-json "会议.asr.json" --samples-list
python scripts/transcribe.py --from-json "会议.asr.json" --samples-accept 0
python scripts/transcribe.py --from-json "会议.asr.json" --samples-reject 1
python scripts/transcribe.py --from-json "会议.asr.json" --continue --resample
```

- 默认位置是所选 `vp_db` 同级的 `声纹采样/`；没有显式库路径则在用户数据目录下。每个最终姓名建立一个经路径净化和摘要消歧的目录；同名多个编号合并选取两段。
- 本次首次归属快照中，最高候选已达到阈值且实际被用作暂定姓名者，按姓名跳过本轮采样和入库，清单为 `skipped`、原因为 `threshold_matched`；不重新匹配，也不因库中仅有同名记录就跳过。该姓名在最终稿中仍沿用时跳过；若人工纠正为另一个姓名，新姓名仍需采样。低分候选即使后来被人工命名，也不算阈值命中。旧批次中仍 pending 的命中者再次继续时转为 skipped，仅删除该组待确认文件，保留其他人的文件、组 ID 和既有 accepted/rejected 决定。
- `pick_spans` 从同身份连续发言选片段，切开超过一秒的空档，排除已知其他人的重叠时间，最长 `vp_segment_seconds`、最短 `vp_min_seconds`。长发言可切为两段；不能保证上游漏检的混入语音被排除，因此必须试听。
- 每次只生成两段；不足两段为 insufficient，不入库。分离稿不受影响，后续可人工指定正确片段重新采样。
- 智能体逐人展示两段本地音频、姓名及原录音时间点，用选择框询问“准确 / 不对/不确定”。两段都准确才接受；一段错误或不确定则拒绝整组。用户未回复保持 pending。未命名者必须先补真实姓名并重新继续/试听，不能将“说话人N”录入库。
- 准确后才对两份 MP3 分别生成向量，一次保存两条记录；来源是可回听的 MP3 文件本身。重复确认去重，不重复计数。接受的 MP3 保留供复核；不对/不确定仅删除该组未入库样本，不删除审核稿或分离稿。
- 清单绑定审核与映射版本、库路径和 MP3 摘要。修改审核/映射、切换库或修改采样文件后，旧确认不能使用。`--continue --resample` 明确生成新批次，重置未完成采样，但保留旧的已入库音频；重新采样不自动移除旧向量。
- 已 accepted/rejected 的决定不能直接反转。对已入库样本的纠错用查询/删除管理；删除向量不顺带删除录音，避免破坏复核证据。拒绝后再次继续不会默默重建被拒绝样本，需要明确 `--resample`。
- 被拒绝的组本轮即结束，不要求补抽或重复提取。直接对原批次其他已试听并接受的组运行 `--samples-accept`，继续询问其余 pending 组；不要为处理拒绝而自动 `--resample`。仅接受/拒绝不改变审核版本；再次 `--continue` 复用本批次，不重新提取。列表不再把 rejected/skipped 组的已删除文件当作可试听链接。
- `accepted / rejected / insufficient / skipped` 都是完成状态。没有 pending 后直接进入会议纪要；全部因阈值命中而 skipped 时也一样，不要求先录入新样本。skipped 无法通过 `--samples-accept` 入库，显式 `--resample` 也继续排除本次阈值已命中的姓名。

## 查询、试听及手工维护

```bash
python scripts/transcribe.py --vp-list
python scripts/transcribe.py --vp-query "张三"
python scripts/transcribe.py --vp-compute "已确认片段.mp3" --vp-start 0 --vp-duration 8
# 仅在用户已经试听并明确确认该段身份后，手工录入才可加此标记
python scripts/transcribe.py --vp-enroll "张三=已确认片段.mp3" --vp-start 0 --vp-duration 8 --vp-confirmed-audio
python scripts/transcribe.py --vp-remove "张三" --vp-index 0
```

查询显示样本完整来源路径，智能体可供用户回听。会议样本优先使用清单确认，不能用手工录入绕过 pending/rejected 状态。原生 `scripts/voiceprint.py --enroll` 同样要求用户确认后才传 `--confirmed-audio`。名单在接受采样后记录，名单保存异常不覆盖旧数据、不丢弃转写结果或已保存声纹。

旧流程曾生成的自动聚合向量没有样本试听证明，不能因升级就视为已审核。用 `--vp-query` 查来源并由用户决定保留/删除；本次更新不自动删除现有个人库。

## 依赖、模型及旧库

可选依赖 `onnxruntime`、`numpy`、`kaldi-native-fbank`，与运行解释器一致。原始结果先保存，再在首次归属时处理三态开关及依赖；校准不重复加载匹配。待决退出 3 后可从 JSON 继续，批准入库时仍需可用的声纹依赖。管理子命令与转写共用 `--config`、模型、阈值、`--vp-db`。

技能包不含模型权重，仅保留模型清单与 Apache-2.0 许可。模型按显式路径、用户缓存、旧项目缓存查找；启用声纹且缺失时下载到用户缓存。固定版本和 SHA256 见 `models/model.json`，默认模型首次加载验证摘要；用户模型需自行核对授权和质量，库记录实际模型摘要。

前处理 `kaldi-fbank-v2`：16kHz、80 mel、dither=0、Povey 窗、逐帧去直流、20Hz 到 Nyquist、逐维减均值。旧 NumPy 前处理向量不能混用；保留旧库，另设新 `--vp-db`，从已确认音频重录，没有原音频无法可靠转换。

余弦分数不是概率，默认 0.74 只是待校准候选阈值。合成音频只能证明执行与一致性，不能证明身份准确率。个人声纹与采样只保留本地，不上传 OSS。

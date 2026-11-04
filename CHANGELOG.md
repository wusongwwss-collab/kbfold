# 更新日志（CHANGELOG）

本文件记录 KBFold 对外可见的变更，格式参照 Keep a Changelog，版本号遵循语义化版本。

## [v0.2.0] · 2026-11-05（feedback 命令面；npm 发版另批）

### 新增

- **`kbfold feedback` 五子命令**（YQ-1154 施工批 D2；契约正本＝开源插件回流规范 v1 §4.2/§5/§6）：
  - `on [L1|L2|L3]`：开启反馈并选档位（默认 L1）——写同意位 `.kbfold/state.json`（consent_asked/level/ts），打印「上行什么 / 永不上行」对照文案；
  - `show`：待上行逐条可看（级别/内容摘要/时间/「尚未发送」态），含本地拒收留痕与已发回执提示；
  - `send`：发送前**逐条打印最终脱敏形态**（只 upload 对象），y/N 确认后才上行——组规范 v1 信封（schema_version="1"/product=kbfold/product_version=仓 version/channel=github_npm/feedback_level/nonce=幂等键/ts/payload）＋HMAC 签名头（`X-OSS-Signature: t=<unix>,v1=HMAC-SHA256(secret, t+"."+raw_body)`）POST 端点；2xx 推进游标（`.kbfold/cursor.json` sent_offset）并写回执码文件 `.kbfold/sentreceipts/<回执码>.txt`（响应含 receipt 则用之，否则 nonce 前 6 位）；
  - `off`：关闭＋90 天静默标记（off_ts），恢复零外连；
  - `clear`：本地清空待上行记录（截断不删文件、不需联网；回执与同意位保留）。
- **发送可靠性语义**：服务端 422（reason 码 `residue:phone|residue:email|forbidden_key:<k>|schema:<field>|whitelist:<k>`）单条永久拒收——本地标 `rejected.jsonl` 并展示 reason 码；429/5xx/网络异常指数退避重试 ≤3 次（失败不推进游标、可续发）；400/401/403/404/413 视为端点/签名配置错误——中止且不烧记录；未开启反馈时 `send` 退出码 3；stdin 非 TTY 且缺 `--yes` 时退出码 4（绝不静默上行）；SDK 本地预检拒收（残留/越权/档位降级）同样永久跳过、零网络请求。
- **环境变量**：`NW_OSS_FEEDBACK_ENDPOINT`（端点覆盖，缺省 `https://www.easurwork.cn/api/oss-feedback/v1`；联调/E2E 用）、`NW_OSS_FEEDBACK_HMAC`（签名密钥；缺省无签头发送并警告）。
- **测试**：`tests/test_cli_feedback.py` 21 例——本地 mock HTTP 端点（标准库 http.server）全链路：on/off/show/clear、信封八字段与 wire 载荷白名单（L1 只含 event；L2+ 禁含 event）、游标推进不重发、回执码留存（nonce 前 6 位与响应回执两形态）、422 拒收标记、5xx 退避计数与中止、HMAC 头逐字节复算、无签名警告、端点 env 覆盖、TTY 降级、audit 豁免面自证。

### 变更

- **`kbfold audit` 自证口径升级**：反馈上行件 `hosts/cli/kbfold_feedback_net.py` 为全仓**唯一**网络面豁免（opt-in、默认关、`feedback send` 时才 lazy import；audit 明示豁免面），其余任何文件命中禁网模块仍即自证失败——默认路径零外连承诺不变。
- `feedback on` 状态文件增 `ts`（同意时间戳）；`show` 升级为规范 §6.2 表格形态（待上行 N 条 · 尚未发送）。
- CONTRIBUTING 增「数据回流与贡献双用途」声明；README 反馈段增命令速查小节；docs/FEEDBACK.md §4 增 send/clear 说明。

### 兼容

- `feedback on --level L2` 旧写法继续可用（新增位置写法 `on L2`）；既有 `state.json` / `feedback.jsonl` 格式不变——游标 / 拒收 / 回执为新增独立文件（`cursor.json` / `rejected.jsonl` / `sentreceipts/`），`.kbfold/` 已加入 .gitignore（本机状态不进公开仓）。

## [v0.1.0] · 2026-11-01（首发）

### 新增

- **四能力判定链**：
  - **扫描**：只读遍历，产出现状统计（可判定 / 待归 / 被排除）与乱度指标、疑似知识库清单（每条带置信与人话理由）；只读文件元信息，不读文件内容，运行前后源目录零变化。
  - **匹配**：四层信号（显式标记 / 名称 / 结构 / 类型）判分，L0–L6 判定梯命中即停；三态分流（直落 / 建议 / 待归）；平手不猜，进 `_inbox/` 并附两条候选与得分。
  - **方案**：`plan.json` 预演（adopt / mkdir / shift / rearrange 四张分列清单，可 diff、可贴 issue、可人工改）；adopt 优先于 mkdir；`apply --rename-only` 只改名不搬家，`--move` 需二次确认；先记账再动手，中途失败整批停。
  - **回滚**：`journal.jsonl` 追加式台账（undo 也是追加记录）；`kbfold undo <批次号>` 逆序回放、逐步 hash 校验、冲突即停，撤销后与动手前逐字节一致。
- **CLI 命令面**：`kbfold plan / apply / undo / index / search / audit / explain / validate / archive / feedback`。
- **MCP 五个只读工具**：`kb.scan` / `kb.plan` / `kb.search` / `kb.explain` / `kb.validate`；`apply` / `undo` / `archive` 刻意不注册为工具——AI 只能预演，落地由人执行。
- **库格式公开规范 v0.1 冻结**（`docs/FORMAT.md`）：`.kb/` 四契约件（`manifest.json` / `journal.jsonl` / `plan.json` / `rules.lock.yaml`）＋ `projection/` 结构契约；`manifest.json` 预留 `pid` 库身份字段；`version:1` 兼容四承诺（字段只增不删不改义 / 第三方可按规范读写 / 破坏性变更升版本位 / `reason` 永远是人话）。
- **规则包 `packs/kbfold/`**：L0 通用模板两件（项目阶段八段 / 文件类型十二类，全字段中文注释，目录规模随文件量伸缩）＋ 规则 DSL 全量示例（探测、路径规则、`@exclude` / `@inbox`、冲突裁决四条、通用名黑名单说明）。
- **反馈 SDK**：三档 L0–L3，**默认关闭（L0）**，首启问一次；开启后上行仅三样统计与结构信息（被拒归档建议三态 / 模板命中计数 / 目录结构 hash 只 hash 不报名）；本地 PII 槽位化＋原文残留检测＋服务端拒原文二次校验；`kbfold feedback on | off | show`。
- **安全自证**：`kbfold audit` 列出本次运行全部外部端点（正常为空或仅本地嵌入模型端口）；删除能力不存在（代码无删文件调用，可自行检索验证）。
- **宿主无关说明书**（`skills/SKILL.md`）与 `examples/` 演示目录（合成数据，断网可复现同一份输出）。

### 承诺口径

- `npx kbfold plan <演示目录> --demo`（npm 包已发布：https://www.npmjs.com/package/kbfold ；Node 壳，克隆仓库则 `node bin/kbfold.js`）：判定内核由 Python core 承担，机器需另备 Python 3.10 及以上。
- 三条红线写进 README 并可实测：不删文件 / 不联网（audit 自证）/ 不猜（inbox 附候选）。

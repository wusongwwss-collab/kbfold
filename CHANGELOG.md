# 更新日志（CHANGELOG）

本文件记录 KBFold 对外可见的变更，格式参照 Keep a Changelog，版本号遵循语义化版本。

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

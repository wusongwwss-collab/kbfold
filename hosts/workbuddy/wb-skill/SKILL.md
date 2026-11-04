---
name: kbfold
display_name: 知识库整备台
display_name_en: KBFold
description: 把散乱的项目资料编译成知识库——一个改错了能一键退回去的知识库整备台
description_zh: 扫描项目资料目录，判定每个文件该归哪个库哪一层，产出可预览的重整计划；先看后动、追加式日志、一键整批回滚；判定不猜，置信不足进待归队列附候选
description_en: Compile scattered project files into a knowledge base you can undo, preview first, append-only journal, one-command rollback
category: 办公效率
version: 0.1.0
author: 弈枢AI Easur Work
---

# 知识库整备台（KBFold）

一个**改错了能一键退回去**的知识库整备台：把散乱的项目资料编译成知识库。

## 什么时候用 / 不用

- 用：项目资料目录乱（散件、旧版、过程稿混放），想按「项目阶段／文件类型」等模板重整，但怕改坏、要能整批回滚。
- 不用：想自动删文件（不存在删除能力）、想让大模型替你决定归属（判定不猜——置信不足进 `_inbox/` 待归队列附候选，人来定）。

## 两条使用路径

1. **免克隆**（npm 包已发布）——`npx kbfold plan <你的资料目录> --demo`（Node 18+ 壳 + Python 3.10+ 内核；先预演，`apply --rename-only` 才落盘，`undo <批次号>` 整批回滚）。
2. **克隆仓库**——`git clone https://github.com/wusongwwss-collab/kbfold.git` 后在根目录 `node bin/kbfold.js plan ./examples/demo/fixtures --demo` 走演示目录。

## 四个能力

| 能力 | 命令 | 边界 |
| --- | --- | --- |
| 扫描 | `kbfold plan`（只读） | 只读元信息不读内容，运行前后源目录逐字节一致 |
| 匹配 | 规则 DSL + L0–L6 判定梯 | 打平不猜；`data`/`docs` 等通用名写进规则启动即报错 |
| 方案 | `kbfold apply --rename-only` | 先记账再动手；中途失败整批停 |
| 回滚 | `kbfold undo <批次号>` | 逆序回放、逐步 hash 校验、冲突即停 |

MCP 形态另有五个只读工具（`kb.scan` / `kb.plan` / `kb.search` / `kb.explain` / `kb.validate`）——apply/undo 刻意不注册为工具，AI 只能预演，落地由人执行。

## 三条可实测的承诺

不删文件（源码无删文件调用）／ 不联网（`kbfold audit` 端点清单正常为空）／ 不猜（低置信进待归附候选）。

## 模板与规范

`templates/` 内含 L0 两套通用模板（项目阶段八段／文件类型十二类，全中文注释）与规则全量示例；`references/FORMAT.md` 是 `.kb/` 库格式公开规范 v0.1 冻结件——第三方可按规范读写接管，不绑定 KBFold。开源仓 github.com/wusongwwss-collab/kbfold（Apache-2.0，npm 包 npmjs.com/package/kbfold）。

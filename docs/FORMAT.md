# KBFold 库格式公开规范 v0.1（FORMAT）

> **状态：v0.1 冻结。** 本文是 `.kb/` 库格式的唯一公开规范。自 v0.1.0 发布起，凡引用库格式一律按「v0.1 冻结」口径表述：任何字段变更＝升 `version` 位，并遵守文首四条兼容承诺。第三方实现只需满足本规范，即可读写 `.kb/` 的格式契约件——我们不加任何私有字段，也不加私有校验。一个能被别人接管的台账，才可能变成事实标准。

## 0. 兼容承诺（先读这四条）

1. **`version: 1` 的库，KBFold 1.x 全系永久可读可写。** 字段只增不删、只增不改义；新增字段对旧读方不可见（读方遇到未知键必须忽略，不得报错）；废弃字段保留但标 `deprecated`。
2. **第三方可按本规范读写 `.kb/` 格式契约件。** 我们不加私有字段、不加私有校验；`.kb/` 不绑定 KBFold，任何满足本规范的工具都可以接管。
3. **破坏性变更必须升 `version` 位**，并保留旧版本读取器一个 major 周期；大版本升级先提示备份 `.kb/`，提供迁移命令，**不做静默迁移**。
4. **`reason` 永远是人话。** 不承诺跨版本措辞稳定，但承诺该字段永远存在、永远是明文自然语言、随时能回答「这个文件为什么在这儿」。

## 1. 冻结面与实现面

`.kb/` 里的东西分两层，规范地位不同：

| 层 | 内容 | 规范地位 |
| --- | --- | --- |
| 格式契约（冻结） | `manifest.json`、`journal.jsonl`、`plan.json`、`rules.lock.yaml` | 本文第 3–6 章逐字段定义，第三方可依赖 |
| 结构契约（冻结） | `projection/` 目录结构与 `_inbox/` 语义 | 本文第 7 章 |
| 实现面（不承诺） | `index.sqlite`、`trash/<batch>/`、`undo/` | 第三方**不应依赖其内部结构**：`index.sqlite` 可重跑重建，`undo/` 可从 journal 重建，`trash/` 有保留期 |

## 2. 目录总览（用户在源目录旁看到的全部）

```
<库根>/
├── kbfold.yml            规则（人写、人读的那份；字段面见规则 DSL 参考）
└── .kb/                  全部产物（删掉即复原；别提交进 git）
    ├── manifest.json     指纹台账：增量与「移动识别」的唯一依据   【格式契约，冻结】
    ├── journal.jsonl     追加式改动日志：undo 的唯一依据          【格式契约，冻结】
    ├── plan.json         本次判定计划：可 diff、可贴 issue、可人工改 【格式契约，冻结】
    ├── rules.lock.yaml   本批生效规则快照 + sha256                 【格式契约，冻结】
    ├── projection/       投影视图（目录 junction 或纯清单）        【结构契约，冻结】
    │   ├── 02-合同/ …    按模板长出的干净视图
    │   └── _inbox/       平手与低置信的落点：每份附候选与理由（待办队列，不是垃圾桶）
    ├── index.sqlite      全文 + 分块索引（可被 DSH / MCP 直接查）  【实现面】
    ├── trash/<批次号>/   原件快照（保留 N 天；清理快照 ≠ 删源文件）【实现面】
    └── undo/             按批次分组的回滚索引（可从 journal 重建）  【实现面】
```

「`rm -rf .kb` 即完全复原」成立的前提是：你不需要撤销已做过的操作。要撤销，先 `undo`。

## 3. `manifest.json`（指纹台账）

v0.1 字段全表。顶层与分片同构（见 3.2）。

### 3.1 字段表

| 字段 | 类型 | 必有 | 含义 |
| --- | --- | --- | --- |
| `version` | int | 是 | 库格式版本，当前恒为 `1`。读方遇到不认识的版本**报错退出，不做猜测性兼容** |
| `pid` | string(uuid) | 是 | **库身份**：项目的稳定编号。库名可改、路径可换、一库多根，pid 不变（v0.1 起预留，生成后不变更） |
| `name` | string | 是 | 人类可读库名（显示用；改它不动 pid） |
| `rules_locked` | string | 是 | 本批生效规则快照的 sha256，对应 `rules.lock.yaml`（两者互相印证） |
| `files` | object | 是 | 文件台账；键＝源文件相对路径（相对库根）。分片存储见 3.2 |
| `files.<path>.size` | int | 是 | 字节数 |
| `files.<path>.mtime` | int | 是 | 修改时间（epoch 秒） |
| `files.<path>.hash` | string | 惰性 | `sha256:…`。仅当 size/mtime 变化后重算——十万文件目录的 IO 闸 |
| `files.<path>.kb` | string | 是 | 归属库短编号（规则文件 `kbs[].id` 的取值） |
| `files.<path>.target` | string | 是 | 投影内目标目录；`@inbox`＝待归 |
| `files.<path>.confidence` | float | 是 | 判定置信（0–1）。残差 ＝ 0.95 − 置信，残差大的就是最该人工看的一批 |
| `files.<path>.reason` | string | 是 | **人话理由**（如 `L0:显式标记 .xhproject`），直接显示在报告与 `kbfold explain` 里。承诺见文首第 4 条 |
| `files.<path>.settled` | bool | 是 | 已定案标记：true 后续批次不再重复判定 |
| `files.<path>.tombstone` | bool | 否 | 文件消失时打标，**绝不删记录**（可撤回） |

示例（单条）：

```json
{
  "version": 1,
  "pid": "3f2a9c1e-7b4d-4e8a-9c21-5d0f6a8b7e02",
  "name": "示例项目",
  "rules_locked": "sha256:8f3c…",
  "files": {
    "示例项目/合同/2024-采购合同-v2.pdf": {
      "size": 412903,
      "mtime": 1730188800,
      "hash": "sha256:1a2b…",
      "kb": "XH-2024",
      "target": "02-合同",
      "confidence": 0.95,
      "reason": "L0:显式标记 .xhproject",
      "settled": false
    }
  }
}
```

### 3.2 分片存储

大目录上 `files` 会很大，因此按**顶层目录切片**存储：

- 主文件 `manifest.json` 保存 `version` / `pid` / `name` / `rules_locked` 与分片索引；
- 每个顶层目录一个分片文件 `manifest.<顶层目录名>.json`，内部结构与 3.1 的 `files` 段同构（键＝该顶层目录下的相对路径）；
- 分片键＝顶层目录名，v0.1 冻结；读方必须能同时接受「单文件整存」与「分片存储」两种物理形态，语义等价。

### 3.3 四态比对语义（读方需要知道的行为契约）

manifest 是增量与「移动识别」的唯一依据。比对结果分四态：

| 态 | 判据 | 处置 |
| --- | --- | --- |
| 新增 | manifest 无记录 | 判定 + 投影 + 建块 |
| 变更 | 同路径，hash 变了 | 重判定，重建该文件的块 |
| 移动 | 同 hash，新路径 | **只改链接，不重判定** |
| 消失 | 有记录，盘上没有 | 打 `tombstone`，**绝不删记录** |

两个防抖机制同属行为契约：`settled` 终态集（人工定案后不再重复判定）；组合键幂等（`batch = hash(规则版本 + 变化集)`，同一规则同一批变化重跑不产生第二批动作）。

## 4. `journal.jsonl`（追加式改动日志）

一行一条 JSON，**追加不改写、不删除**。这是 undo 的唯一依据。

```json
{"batch":"20261101-1412-a3f9","op":"rename","from":"合同/a.pdf","to":"06-合同与协议类/2024-星海-采购合同-v2.pdf","hash_before":"sha256:1a2b…","hash_after":"sha256:1a2b…","ts":1730189520}
{"batch":"20261101-1412-a3f9","op":"undo","from":"06-合同与协议类/2024-星海-采购合同-v2.pdf","to":"合同/a.pdf","hash_before":"sha256:1a2b…","hash_after":"sha256:1a2b…","ts":1730191200}
```

| 字段 | 含义 |
| --- | --- |
| `batch` | 批次号（`日期-时间-短随机`），apply 结束时当场打印；undo 按批次整批回放 |
| `op` | 动作，闭集 ∈ {`rename`, `move`, `mkdir`, `adopt`, `undo`}；**未知 op 视为格式损坏，拒绝回放** |
| `from` / `to` | 动作前后路径（相对库根） |
| `hash_before` / `hash_after` | 动作前后内容指纹；undo 逐步校验的依据 |
| `ts` | 记录时间（epoch 秒） |

行为契约：

- **撤销也是一条 append 记录**（第二行的 `op:"undo"`），不是把原记录删掉。把 journal 从头读一遍，能重放到「现在的真实状态」。
- undo 三规则：**逆序回放**；**每步先校验当前 hash 与 `hash_after` 一致，不一致即停、报冲突、不覆盖**；**回滚目标被占落 `<原名>.restored-<时间戳>` 并列入报告**。
- 三条禁令：禁尽力而为式回滚、禁盲覆盖、禁无 batch 的散装写入。

## 5. `plan.json`（本次判定计划）

`plan` 的产物，纯预演、零写入。可 diff、可贴 issue、可人工改后交给 `apply` 执行。`version` 恒为 `1`；读到不认识的版本**报错退出，不做猜测性兼容**。

v0.1 顶层键面（字段只增不删）：

| 键 | 类型 | 含义 |
| --- | --- | --- |
| `version` | int | 恒为 `1` |
| `root` | string | 扫描根（POSIX 风格路径） |
| `batch` | string | 本次 apply 将产生的批次号（组合键幂等：`hash(规则版本 + 变化集)` 加时间戳） |
| `rules_locked` | string | 规则快照指纹（`sha256:…`），与 `rules.lock.yaml`、manifest 的 `rules_locked` 互相印证 |
| `adopt` | list | 既有容器纳入清单，元素 `{path, as}` |
| `mkdir` | list | 需新建目录清单，元素 `{path}` |
| `renames` | list | 拟改名平移清单（rename-only 档），元素 `{from, to, kb, confidence, reason}` |
| `inbox` | list | 待归清单（不猜，附候选），元素 `{from, candidates, reason}` |
| `excluded` | list | `@exclude` 清单（不进投影、仍记指纹），元素 `{from, reason}` |
| `stats` | object | 六项计数：`{files, adopt, mkdir, renames, inbox, excluded}` |

示例（节选自演示目录实跑产物，逐键同构）：

```json
{
  "version": 1,
  "root": "~/Projects",
  "batch": "20261101-1412-a3f9",
  "rules_locked": "sha256:8f3c…",
  "adopt":    [ { "path": "06-合同与协议类", "as": "06-合同与协议类" } ],
  "mkdir":    [ { "path": "07-流程与制度类" } ],
  "renames":  [ { "from": "散件/b.pdf", "to": "06-合同与协议类/b.pdf", "kb": "XH-2024", "confidence": 0.95, "reason": "L0:显式标记" } ],
  "inbox":    [ { "from": "散落资料/随手记.txt", "candidates": [ { "kb": "YY-2025", "target": "04-图纸" } ], "reason": "路径规则平手（不猜）" } ],
  "excluded": [ { "from": "旧库/过程稿/合同-草稿-旧版.md", "reason": "L0:显式标记" } ],
  "stats": { "files": 7, "adopt": 1, "mkdir": 1, "renames": 1, "inbox": 1, "excluded": 1 }
}
```

行为契约：

- **五张分列清单**：动作三张——`adopt`（既有容器纳入）/ `mkdir`（新建目录）/ `renames`（改名平移，v0.1 只提供 rename-only 档）；不动两张——`inbox`（待归，附候选与理由）/ `excluded`（不进投影、仍记指纹）。分列即可分别审阅、分别放行。
- **`adopt` 优先于 `mkdir`**：目标目录已存在 ⇒ 进 `adopt` 清单（不改名不移动，认作既有容器）；只有真正缺口才出现在 `mkdir` 清单。
- 已在目标位置的文件不进任何清单（不动）。
- `batch` 是顶层字段＝**预览时就把批次号给出来——退路在预览时就给**；apply 按该批次先记账再动手，undo 按批次整批回放。
- 平手与低置信项不进动作清单，落 `inbox`（对应 `projection/_inbox/`）并附候选与理由。

## 6. `rules.lock.yaml`（规则快照）

内容 ＝ 那一批实际生效的规则全文 + 它的 sha256，与 manifest 的 `rules_locked` 字段互相印证。

v0.1 落盘形态（逐字）：**两行注释头 + 规则全文**：

```
# kbfold rules.lock（本批生效规则快照）
# sha256: 8f3c…
<规则全文>
```

- `# sha256: ` 行 ＝ 规则全文的 SHA-256 十六进制值（指纹全形 `sha256:8f3c…`，与 manifest 的 `rules_locked` 同值；读方按该行取指纹）。
- `<规则全文>` ＝ 本批实际生效规则的**规范序列化文本**（v0.1 实现统一按 JSON 序列化落盘，无论规则来源是 `kbfold.yml` 还是 `.json`；sha256 即对这段文本逐字计算，可复核）。文件扩展名沿用 `.yaml`，读方按「两行注释 + 全文」解析，不要求 YAML 结构面。
- 落盘时机：`plan`（非 `--demo`）与 `apply` 各写一次，恒覆盖为当前批次生效规则。

删了它不影响运行，但会让历史判定变成「无法复现」。半年后想知道「这份合同当初按什么规则归到这里的」，只能靠它。

## 7. `projection/`（投影视图，结构契约）

- 投影是**库的编译产物**，不是第二份真身：源目录之外的全部可见结果都长在这里（目录 junction 或纯清单，Windows 上免管理员）。
- 目录名来自模板与规则的目标（如 `06-合同与协议类/`）。
- `_inbox/` 是平手与低置信的落点：每份附候选与理由。**它是待办队列，不是垃圾桶**——你处理一批（改规则或手动安置）再跑 `plan`，它才会变短；它不自动清空，因为「自动清空」等于「自动猜」。
- `@exclude` 命中的内容**不进投影**，但**仍登记指纹**（manifest 里有它）——将来把它从排除清单里拿掉，不必重扫全盘。

## 8. 跨版本行为（读方义务与我们的义务）

| 场景 | 规定行为 |
| --- | --- |
| 读到认识的 `version: 1` | 按本规范读写 |
| 读到不认识的更高 `version` | **报错退出**，不做猜测性兼容；提示先备份 `.kb/` 再升级工具 |
| 读到未知字段键 | 忽略，不得报错（字段只增不减的必然要求） |
| journal 出现未知 `op` | 视为格式损坏，**拒绝回放**（宁可停下，不可猜） |
| 破坏性变更 | 必须升 `version` 位；旧版本读取器保留一个 major 周期；提供迁移命令，不做静默迁移 |

## 9. 实现面文件（第三方不应依赖）

- `index.sqlite`：全文 + 分块索引，可被 DSH / MCP 直接查，但结构随版本演进；删掉重跑即重建。
- `trash/<批次号>/`：原件快照，有保留期；**清理快照不等于删源文件**。
- `undo/`：批次索引，丢了可从 journal 重建。

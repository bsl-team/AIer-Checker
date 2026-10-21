# cf — 代码风格内核

从代码样本里提取作者的书写风格，检查风格是否自相矛盾，按用户保存风格档案，并据此重新排版代码。
**内核不含任何界面**：全部能力通过 `cf` 命令行或 `import cf` 提供；`demo/` 下是一个可选的
tkinter 演示程序，不属于内核。

零第三方依赖，只用 Python 标准库（Python 走 `ast` + `tokenize`，其余语言走内置的 clike 词法器）。

```
pip install -e .                      # 提供 cf 与 cf-demo 两个命令
cf extract src/*.cpp                  # 这批代码体现了什么风格
cf check src/*.cpp --fail-on medium   # 内部有没有自相矛盾
cf profile add bin --lang cpp src/*.cpp
cf format --user bin --write src/*.cpp
```

---

## 1. 硬保证（内核契约）

下面每一条都由代码强制或被测试覆盖，违反任何一条都算 bug：

1. **格式化永不改变 token 流。** 结果文本会被重新分词，与输入的 token 序列逐项比对
   （字符串按值比较——引号互换算排版；注释按词序比较——缩进算排版）。一旦不一致，
   **返回原文**并给出拒绝原因，`cf format` 对该文件退出码 2。宁可不动手，也不产出可能改变
   程序含义的结果。可用 `cf.token_identity(src, path)` 自行校验。
2. **格式化是幂等的：一趟就到不动点。** 同一档案下 `format(format(x)) == format(x)`，
   因此 `--write` 之后紧接 `--check` 必须返回 0。折行产生的新续行也要在第一遍就落在
   indent 阶段会选定的缩进上。
3. **只动空白与行的位置，不增删 token。** 需要插入/删除 token 的规则（尾逗号、分号、改名、
   增删 `const`、逗号与分号互换、调整 include 顺序、改写注释文本）**一律只报告、不改写**，
   见 §4 的"不改写"清单。
4. **风格判定基于证据量，不基于单点。** 每条轴的取值是一个分布，置信度
   `一致率 × total/(total+k)`（k 按轴类型取值），1 次观测永远不足以被称为"风格"。
   低于 `--min-confidence`（格式化默认 0.5）的已观测规则不动手。
5. **规则名与机器输出与语言无关。** 57 条轴名保持 ASCII，`--json` 输出、档案文件、
   `--rules` 过滤参数都不随提示语语言变化；只有给人看的报告文字会被本地化。
6. **读什么编码就写回什么编码。** UTF-8/GBK/CP936/BIG5/CP1252/LATIN-1 依次试探，
   BOM 与 UTF-16 显式识别；`--write` 按原编码写回，不会给原本无 BOM 的文件加上 BOM，
   也不会把 LF 文件改成 CRLF（除非你显式传 `--eol crlf`）。
7. **提示语可切中英文。** 默认英文；`-L/--msg-lang zh`（可出现在命令行任意位置）或
   环境变量 `CF_LANG=zh` 切换，覆盖报告、错误信息与 `--help`。

## 2. 公开 API

```python
import cf

cf.extract(doc) -> Profile                 # 单个文档
cf.extract_all(docs) -> Profile            # 多样本合并成一个共识档案
cf.build_document(src, path=None, spec=None) -> Document
cf.analyze_consistency(docs, baseline=None, ...) -> ConsistencyReport
cf.ProfileStore(path=None)                 # 用户 -> 风格档案
cf.format_code(src, profile, path=None, options=None) -> FormatResult
cf.token_identity(src, path=None)          # 契约 1 用的比较基准
cf.set_locale("zh") / cf.get_locale() / cf.t(key, **kw)
```

`FormatResult` 带 `text / changed / applied / skipped / error`：`applied` 是真正改动了的规则，
`skipped` 是因证据不足没动手的规则，`error` 非空表示拒绝执行（此时 `text` 就是原文）。

## 3. 命令行契约

| 命令 | 作用 | 备注 |
|---|---|---|
| `cf extract FILES…` | 输出风格档案 | `--json` / `--show-rows` / `--min-confidence` / `--user` 顺手入库 |
| `cf check FILES…` | 一致性审计 | `--user`/`--profile` 改为对照档案；`--fail-on`、`--show-rules`、`--all` |
| `cf profile <子命令>` | 档案管理 | `add/update/list/show/delete/rename/export/import` |
| `cf format FILES…` | 重新排版 | 默认只预览；`--write`/`--check`/`--diff`/`--rules`/`--observed-only`/`--no-wrap`/`--eol` |
| `cf rules` / `cf languages` | 查词表与支持度 | |

* **退出码**：`0` 成功；`1` 发现了问题（`check` 达到 `--fail-on` 阈值，或 `format --check` 有改动）；
  `2` 用法或输入错误（含"无法安全格式化"的文件，见 `stderr`）。
* **不给文件名或给 `-` 就读标准输入**，`cf format -` 把结果写到标准输出。
* **命令行自己展开 glob**（`src\*.cpp`），因为 cmd.exe 和 PowerShell 不会替你展开。
* 档案库路径优先级：`--store` > 环境变量 `CF_STORE` > `~/.code-feature/profiles.json`。

## 4. 规则词表：57 条轴，33 条会动手

`cf rules` 是权威清单。按组粗分：

| 组 | 例子 | 会改写 | 只报告 |
|---|---|---|---|
| 缩进 | `indent.type` `indent.size` `indent.continuation` `indent.namespace_body` `width.limit` | ✅ | |
| 大括号 | `brace.style` `brace.close_same_row_branch` `brace.empty_block` | ✅ | |
| 空白 | `space.*`（含 `space.before_template_angle`、`space.inside_template_angle`） | ✅ | |
| 空行 | `blank.*` | ✅ | |
| 引号 | `quote.style` | ✅（需 ≥0.85 一致率） | |
| 折行布局 | `layout.case_indent` | ✅ | `layout.trailing_comma` `layout.wrap_operator` `layout.single_statement_same_row` |
| 注释 | `comment.style` `comment.doc_style` `comment.block_ratio` | | 注释文本永不改写 |
| 命名 | `naming.function/method/variable/parameter/constant/type/member/macro` | | 改名改变的是程序 |
| 常量写法 | `constant.marker`（const / constexpr / #define / 枚举）`constant.placement`（`const int` vs `int const`） | | 增删或移动 `const` |
| 并句习惯 | `statement.comma_join`（`cin.tie(0),cout.tie(0);` vs 分成两条 `;`） | | 逗号与分号互换改 token |
| 组织 | `include.order` `include.grouping` `semicolon.required` | | 顺序影响构建、ASI |

"只报告"的意思是：这些轴的**不一致会被 `check` 指出、在 `extract` 里可见**，但格式化器不碰它们，
因为做不到"只动空白"。

## 5. 一致性严重度

`cf check` 的问题分级不是主观打分，而是按轴的性质：

* `STRICT_ANY_DEVIATION`（`indent.type` `indent.size` `blank.trailing_whitespace` `width.limit`
  `quote.style`）—— 文件内出现 1 处偏离就算问题，等级取自固定表（缩进 `high`、行宽/引号 `medium`、
  行尾空白 `low`）。
* `CONTENT_DRIVEN`（引号、续行缩进、参数排布、尾逗号、折行运算符、部分命名轴、`constant.marker`）——
  作者常常按内容选择写法，文件内混用只算 `low`。
* 其余：文件内混用按一致率给 `low`/`medium`；对照档案时命中"高影响轴"（缩进、大括号、命名、
  `const`/分号、`space.after_keyword` 等）给 `high`（`--user` 场景）或 `medium`。

## 6. 支持的语言

`c cpp go javascript typescript java rust csharp shell python clike`。
带扩展名时按扩展名判定；无扩展名（例如标准输入）按内容启发式，兜底是 `clike`——
纯数据文件（没有任何 `def`/`class`/`import`）无法靠内容判定，请显式 `--lang`。
Python 支持最全（`tokenize` + `ast` 双路），其余语言共用一套 clike 词法器并按语言配关键字表。

## 7. 契约是怎么被验证的

* 单元/端到端测试 215 条（词法、文档、提取、一致性、档案存储、格式化、CLI、GUI 服务层、i18n）。
  其中成组地跑真实样本，断言"格式化后 token 不变""再格式化一次没有新改动"；并用受控的故障注入
  证明 token 保险确实会触发（一旦改动会丢 token，就返回原文并给出拒绝原因）。
* 大规模语料回归：Python 标准库 185 个真实文件，以及 864 个真实 OI 解答 `.cpp`
  （各自档案 + 共享档案两轮），均达到 **0 崩溃 / 0 拒绝 / 0 改 token / 0 非幂等**。
* 有意保留的采集空白，是为了不产生假证据：函数式宏（`#define SQ(x)`）、成员函数尾随的
  `const`、`#include <...>`、语言规定的入口名 `main`、dunder 等都不算作者的书写选择。

## 8. 已知边界（不是 bug，是取舍）

* **证据不足时会用语言族默认值动手。** 这是 `use_defaults` 的默认行为，实测代价不小：
  在一份 864 文件的语料里，`blank.between_functions` 的 723 次改动绝大多数来自默认值而不是样本。
  想"绝不臆造风格"就加 `--observed-only`；只想动手高置信规则就调 `--min-confidence`。
* `space.before_template_angle` 对 clike 的默认值是"紧贴"（`vector<int>`），而某些人群
  （竞赛代码实测 88%）习惯写 `vector <int>`。样本里没有模板时，默认值会与他们的习惯相反。
* `quote.style` 对 C/C++/C# 不采集：这些语言里没有单引号字符串可选，不存在选择。
* C++ 的 `>>` 是一个 token，`vector<vector<int>>` 内外层共享同一个闭合符，内侧空格按整体处理。
* Windows 上 `cf format -` 经标准输出写结果时走文本模式，管道里 LF 会变成 CRLF；
  `--write` 原地改写文件不受影响（见契约 6）。

## 9. 开发

```
pip install -e .
python -m pytest -q                 # 215 个测试
python -m cf.cli --help             # 未安装也可以直接跑
cf-demo                             # 可选的 tkinter 演示（不属于内核）
```

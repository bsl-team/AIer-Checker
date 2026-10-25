# AIer Checker — 本地教师代码审查台

从比赛文件夹批量读取学生提交，检查同一位学生在一场比赛中是否出现多套可区分风格、文件之间是否
存在结构性重复，并可与学生的历史风格档案对照。**代码只在本机读取和分析，不会上传。**

项目保留 `cf` 分析内核，唯一入口是本地 Web 审查台；内核能力仍可通过 `import cf` 使用。

零第三方依赖，只用 Python 标准库（Python 走 `ast` + `tokenize`，其余语言走内置的 clike 词法器）。

## 快速开始

```bash
pip install -e .
cf-web --open
# 或直接从仓库运行：
python -m cf.web --open
```

服务默认只监听 `127.0.0.1:8765`，启动后浏览器会打开审查台。常用参数：

```bash
cf-web --port 0                 # 自动选择空闲端口
cf-web --store D:\review\profiles.json
```

档案库路径优先级：`--store` > 环境变量 `CF_STORE` > `~/.code-feature/profiles.json`。

比赛文件夹按学生分目录，每个学生的多道题放在同一目录下：

```
Contest/
  User1/
    T1.cpp  T2.cpp  T3.cpp  T4.cpp
  User2/
    T1.cpp  T2.cpp  T3.cpp  T4.cpp
```

支持 `.c .h .cc .cpp .cxx .hpp .hh .hxx .ino`、`.go`、`.js .jsx .mjs .cjs`、
`.ts .tsx`、`.java`、`.rs`、`.cs`、`.py .pyw .pyi`、`.sh .bash .zsh`。
递归扫描学生目录；隐藏目录、依赖目录和构建目录会被跳过。

## 推荐工作流

1. **先用往期比赛建档**：扫描往期比赛，在“档案库”里点“保存本次全部学生”，选择“合并”。
2. **再扫描本次比赛**：在顶部输入本次比赛文件夹，点“扫描文件夹”。
3. **先看“重点关注”**：按关注等级、风格组、档案偏离和重复程度排序。点进学生可以查看：
   * 同一学生一场比赛里是否出现多套可区分风格（至少 3 个文件才做强判断）；
   * 与本人历史档案的一致性（如果档案包含本次文件，会明确提示“自证，仅参考”）；
   * 本次文件之间的结构重复，以及跨用户重复；
   * 57 个风格轴的本次取值、置信度、证据数和历史值。
4. **在“查重”页看行段证据**：点击“证据”左右并排查看两端的行段附近的源码。
5. **导出 Markdown 报告**：顶部“导出报告”，包含重点关注学生、重复对和免责声明，不导出完整源码。

## 审查台页面

| 页面 | 内容 |
|---|---|
| 比赛总览 | 学生数、提交数、重复对、多套风格人数、高关注人数；重点关注列表；全部学生表 |
| 学生审查 | 本次一致性、风格组、档案一致性、重复对、文件表、风格偏离、57 个风格轴 |
| 查重 | 跨用户/同用户重复对、Jaccard / containment / 共享 k-gram、聚类、行段证据抽屉 |
| 档案库 | 已保存档案、样本数、置信特征数、更新时间；支持单个或批量保存 |
| 设置 | 档案库路径、查重灵敏度、同题比较、是否包含同用户、语言、重新扫描 |

保存档案支持两种方式：

* **合并**（推荐）：只追加档案中尚不存在的文件，按绝对路径判断，避免同一文件重复计数。
* **覆盖**：只用本次比赛的文件重建该学生档案，会丢失之前保存的历史样本。

## 判定口径

* **多套风格是统计线索，不是 AI 生成概率。** 系统比较同一学生各文件在 57 个风格轴上的分布，
  只有组内足够一致、组间差异足够大时才标记为多套风格；界面使用“关注等级 / 建议复核”，
  不输出“AI 判定”。
* **关注等级只决定先看谁。** `高关注` / `建议复核` / `常规` 由多套风格强度、历史档案偏离、
  跨用户重复、本人文件重复等信号组合得出；原因会逐条列出，供人工复核。
* **查重默认只比较同一题目**，避免 `T1` 与 `T4` 误配；可在设置中关闭。改名、空白、注释和
  字面量差异会被规范化抹掉。默认跳过少于 80 token 的提交，并在查重页显示告警。
* **历史档案可能“自证”。** 如果保存的档案已经包含本次文件，再用它比较本次文件，一致性会
  虚高；界面会显示档案重叠数，并降低该信号的参考价值。请用往期比赛建档。
* **所有信号都不是结论。** AI 生成、抄袭与否必须由教师结合过程记录、版本历史和人工访谈判断。

## 内核 API

Web 层复用 `cf` 内核；需要脚本化时可以直接 `import cf`：

```python
import cf

cf.extract(doc) -> Profile
cf.extract_all(docs) -> Profile
cf.build_document(src, path=None, spec=None) -> Document
cf.analyze_consistency(docs, baseline=None, ...) -> ConsistencyReport
cf.ProfileStore(path=None)
cf.format_code(src, profile, path=None, options=None) -> FormatResult
cf.find_duplicates([cf.Submission(...)], **thresholds) -> SimilarityReport
cf.compare_documents(a, b, **thresholds) -> Similarity
cf.fingerprint(doc, k=7, window=4, winnow=True) -> Fingerprint
```

所有查重与一致性对象都带 `to_dict()`，可直接 `json.dumps`，输出顺序确定。
`FormatResult` 带 `text / changed / applied / skipped / error`：`error` 非空表示拒绝执行，
此时 `text` 就是原文。

## 硬保证

1. **格式化永不改变 token 流。** 结果会被重新分词并与输入逐项比较；一旦不一致，返回原文并给出
   拒绝原因。宁可不动手，也不产出可能改变程序含义的结果。
2. **格式化幂等。** 同一档案下重复格式化到达不动点，只动空白与行的位置，不增删 token。
3. **风格判定基于分布与证据量。** 每条轴的取值是分布，置信度为 `一致率 × total/(total+k)`，
   单次观测不足以被称为“风格”。
4. **规则名与机器输出与语言无关。** 57 条轴名保持 ASCII，JSON、档案文件和过滤参数不随提示语
   语言变化；只有给人看的报告文字会被本地化。Web 界面固定中文。
5. **读什么编码就写回什么编码。** 支持 UTF-8/GBK/CP936/BIG5/CP1252/LATIN-1，识别 BOM 与
   UTF-16，不擅自添加 BOM 或改变换行。
6. **查重不联网、不执行被检查的代码、不把完整源码写进报告。** 证据只有行段与 token 数；
   分数只用于排队给人看。
7. **指纹跨进程稳定。** 规范化抹掉改名、空白、注释和字面量差异；指纹使用 FNV-1a 64 位而不是
   Python 的 `hash()`。

## 支持的语言

`c cpp go javascript typescript java rust csharp shell python clike`。

带扩展名时按扩展名判定；无扩展名时按内容启发式判断，兜底为 `clike`。纯数据文件（没有
`def` / `class` / `import` 等结构）无法靠内容判定，需要在设置中显式选择语言。
Python 支持最全（`tokenize` + `ast` 双路），其余语言共用一套 clike 词法器并按语言配置关键字表。

## 已知边界（不是 bug，是取舍）

* **证据不足时会用语言族默认值动手。** 这是格式化的默认行为；想“绝不臆造风格”需要使用
  `observed_only`，或提高 `min_confidence`。
* `space.before_template_angle` 对 clike 的默认值是“紧贴”（`vector<int>`），而部分竞赛选手
  习惯写 `vector <int>`；样本里没有模板时，默认值可能与习惯相反。
* `quote.style` 对 C/C++/C# 不采集：这些语言里没有单引号字符串可选。
* C++ 的 `>>` 是一个 token，`vector<vector<int>>` 的内外层共享同一个闭合符。
* Python 的 token 保险不含 `INDENT` / `DEDENT`：缩进层级由 `python_levels` 重建，
  “层级变了但 token 序列不变”的错误抓不到。
* 折行只在单个括号组的顶层逗号处尝试：不保证每行都压进 `width.limit`，单个超长 token 也不会
  被切开。
* `Profile.merge` 合并 `rows` 时不记录来源文件：多样本档案里的行号只表示“某个样本的第几行”，
  跨文件定位请使用审查台逐文件结果。
* 编码探测按 UTF-8 → GBK → CP936 → BIG5 → CP1252 → latin-1 顺序尝试：某些 CP1252 字节会先被
  GBK 解成错字。
* 查重聚类是**传递闭包**：A≈B、B≈C 会把三份并成一组，即使 A 与 C 并不相似；报告会以
  `transitive-clusters` 提示。
* 多套风格检测需要共同观测特征；代码过短、语言混杂或样本太少时会显示“证据不足”，不会硬判。

## 开发

```bash
pip install -e .
python -m pytest -q
python -m cf.web --open
node --check cf/webui/app.js    # 可选：检查前端脚本语法
```

主要代码：

```
cf/classroom.py      # 扫描、风格分组、关注等级、查重、档案保存
cf/web.py            # 本地 HTTP 服务与 API
cf/webui/            # 原生 HTML/CSS/JS 审查台
tests/test_classroom.py
```

内核模块（`extract`、`profile`、`consistency`、`similarity`、`store`、`format`、`document`、
`tokens`、`report`、`i18n`、`sources` 等）保持独立，Web 层只调用它们。

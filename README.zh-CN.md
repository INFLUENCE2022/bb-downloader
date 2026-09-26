# bb-downloader

把 BlackBoard 账号下**所有课程**的资料批量下载到本地，按学期归档，目录结构镜像
BlackBoard 上的内容层级。

针对 BlackBoard Learn **Classic / Original** 版式（非 Ultra）。复用已登录的浏览器会话，
因此能走 SSO / ADFS 登录，不需要任何 API key。

> English docs: [README.md](README.md)

## 功能

- 枚举账号下**全部**课程（跨所有学期）
- 通过 BlackBoard REST 接口遍历每门课的完整内容树
- 落盘时镜像该层级：`<年份>年<学期>/<课程>/<内容区>/<文件夹>/<文件>`
- 下载讲义、PPT、notebook、数据集、压缩包——能下的全下
- 下载作业附件，并为每门课生成作业索引
- 外链（Zoom 等）只记录不下载
- 支持断点续爬：状态文件让重跑自动跳过已完成的课

## 环境要求

- Python 3.9+
- 已安装 Microsoft Edge（或 Chromium）
- 一个你能在浏览器里登录的 BlackBoard 账号

依赖固定在 `requirements.txt`。注意 `DrissionPage==4.2.0b20` 是**刻意选的 beta 版** ——
脚本用了 4.2+ 的 API，而 PyPI 上没有 4.2+ 稳定版，`pip install "DrissionPage>=4.2"` 会报
No matching distribution。

```bash
pip install -r requirements.txt
```

## 配置

```bash
cp configs/paths.example.json configs/paths.json
```

然后编辑 `configs/paths.json`：

| 字段 | 说明 |
|---|---|
| `base_url` | 你学校的 BlackBoard 地址，如 `https://bb.your-university.edu` |
| `browser_path` | Edge/Chromium 可执行文件路径 |
| `profile_path` | **独立**的浏览器 profile 目录（存登录态） |
| `port` | 本地 CDP 端口，空闲即可 |
| `download_root` | 学期文件夹的存放位置，相对项目根目录 |
| `login_timeout` | 等你完成登录的秒数 |

`profile_path` **不要**指向你日常用的浏览器 profile —— 脚本启动时会杀掉占用该
profile 的进程。

## 用法

```bash
python bb_crawler.py --dry-run           # 只列课程与学期映射，不下载任何文件
python bb_crawler.py                     # 全量下载（自动跳过已完成的课）
python bb_crawler.py --course _12345_1   # 只跑一门课（可重复指定）
python bb_crawler.py --force             # 清空后重下 —— 本次计划里的**每一门**课（保留 homework/）
python bb_crawler.py --update            # 增量：只下载新增/被修改的内容
python bb_crawler.py --update --dry-run  # 预览 --update 会下载什么
python bb_crawler.py --select            # 勾选要下载哪些课程
python bb_crawler.py --exclude _12345_1  # 本次跳过一门课（不改配置文件）
python bb_crawler.py --clean-profile     # 清理浏览器 profile（保留登录态）
```

**务必先跑 `--dry-run`**。它会显示每门课将被放进哪个文件夹而不下载任何东西 ——
这是确认学期映射是否正确最快的方式。

首次运行会打开浏览器窗口要求登录一次，之后登录态持久化在 `profile_path`，后续运行无人值守。

## 增量更新（--update）

`--update` 重新走一遍每门课的内容树，只下载新增或被改过的内容，可以无人值守按计划跑。

**判据**：BlackBoard 树里每个条目都带 `modified` 时间戳，而且**老师编辑课程时它确实会变**
—— 包括往已有条目里增补或替换附件。爬虫把 `条目id → modified` 记进
`.content_snapshot.json`，比对结果决定动作：

| 情况 | 动作 |
|---|---|
| 条目 id 不在快照里 | 新增 → 下载 |
| `modified` 变了 | 已修改 → 重新下载 |
| 条目的父路径变了 | 位置变更 → 下到新位置 |
| 其余 | 跳过 —— **完全不打开该条目的页面**，省时间主要省在这里 |

**几条让它不会骗人的纪律**：

- 一个条目的**全部附件都下载成功**之后才写进快照。失败的不写，这样下周会重试，
  而不是被当成「已见过、没变化」永久漏掉。
- 走内容树时只要有一步取数失败，**这门课的快照就整门不更新** —— 不完整的树不能被当成
  「这些条目消失了」。
- 快照是**合并**而不是替换：老师临时隐藏的条目不返回时，记录保留，重新放出后会再比对。
- 文件名只有浏览器知道的那些类型，先下到临时目录、再按服务端真名挪到目标位置，
  所以重下已修改的文件是**覆盖**，不会在旧文件旁边留一个 `_1` 副本。

**已知盲区**：`modified` 跟踪的是「条目被编辑」。如果有人直接在课程文件库里用同名文件覆盖
（不走条目编辑），时间戳不动，就检测不到。要发现这种改动只能每轮把全部附件重取一遍
（REST 接口对内容文件不提供大小或校验和字段），而那正是这个模式要避开的开销。

**第一次跑**：某门课已经下过但还没有快照时，爬虫会**建立种子快照、不下载任何东西** ——
但会补下「该课上次抓取之后被改动过」的内容。所以首次 `--update` 很便宜，也不会重下已有的归档。

## 选择下载哪些课程

```
python bb_crawler.py --select
```

会列出账号下的全部课程并编号，问你想要哪些：

```
   1. [✓] 2025年下学期 / CS101:程序设计基础_L01
   2. [ ] 2025年下学期 / GEN200:通识选修_L04
   ...
  输入要下载的编号，逗号分隔，支持区间（例：1,3,5-7）
  回车 = 放弃；all = 全部下载；none = 全部不下载
  >
```

它把**补集**写进 `configs/courses.json` —— 也就是你没勾的那些，格式是 `course_id: 课程名`。
三个要知道的后果：

- **新出现的课程默认会被下载。** 文件记的是"跳过什么"，所以下学期新开一门课不需要你做什么
  就会自动抓。反过来存白名单的话，新课会静默地永远不下，而你唯一的症状是一个你从没想过去看的空文件夹。
- **磁盘上什么都不会删。** 取消勾选只是停止后续下载，原来的文件夹原样留着。以后重新勾上，
  靠快照就只补这段期间的变动。
- **计划任务也认这个选择**，因为它在文件里而不是在终端里。这正是它必须是个文件的原因。

`--exclude <course_id>` 只对本次运行跳过某门课，不改文件。`--course <course_id>` 相反：
**只**跑这几门，且优先于已保存的名单。`--dry-run` 会显示每门课当前的状态
（`[已排除，不下载]`）和它的 `course_id`，ID 从那里复制。

文件就是普通 JSON，可以直接手改（写成纯 ID 列表也认）。删掉它 = 全部下载。
它被 gitignore 排除，因为里面有你的真实课程名；入库的是模板 `configs/courses.example.json`。

## 定时运行

`scripts/run_weekly.bat` 跑 `--update`，日志追加到 `logs/update.log`，并传递有意义的退出码：

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 意外错误 |
| 2 | 需要人工登录（会话过期） |
| 3 | 有下载失败 —— 这些条目**没有**记进快照，下次自动重试 |
| 4 | 已有另一个实例在跑 |

Windows 上注册（每周六、周日 20:00）：

```
schtasks /create /tn BB_WeeklyUpdate ^
         /tr "<仓库路径>\scripts\run_weekly.bat" ^
         /sc weekly /d SAT,SUN /st 20:00 /f
```

关于默认的任务设置，两点要知道：

- 任务以 `InteractiveToken` 运行，也就是**只在你登录 Windows 时才会触发**。关机或睡眠时不跑，
  也没有「错过就补跑」。一周跑两次 + 增量逻辑幂等，是用来兜住这个的。
- 要勾上「如果任务已在运行，则不启动新实例」。爬虫自身也加了 `.crawl.lock` 做同样的防护 ——
  两个实例会互相杀掉对方的浏览器（启动时会杀掉占用该 profile 的所有 msedge）。

报告写在 `reports/update_YYYY-MM-DD.md` 和 `reports/latest-update.md`，而且**每次运行**
都会写一份 —— 包括以退出码 2/1 结束的那种。报告缺失或日期很旧，意味着任务压根没启动，
不是「没有变化」。每条记录是下面之一：

| 标记 | 含义 |
|---|---|
| 新增 / 已修改 / 位置变更 | 已抓取并落盘 |
| 补访 | 回访一次以补上缺失的落盘记录 |
| ⚠️ 本次没拿到 | 发现了但没取到，**不记入快照**，下次自动重试 |
| ❓ 需人工确认 | 既没有可下载附件、也没有正文的条目 |

⚠️ 那几行才是要看的：它们是爬虫唯一知道自己漏了东西的情况。

## 产物结构

```
<download_root>/
├── 2025年下学期/                       # 学期文件夹（名字由学期映射决定）
│   └── CS101_程序设计基础 (2026 Fall)/
│       ├── _作业清单.md                # 该课的作业索引
│       ├── homework/                   # 你自己的作业答案（爬虫不管，--force 也不删）
│       │   └── Assignment1.docx
│       ├── Assessment/
│       │   └── assignment1.ipynb       # 作业附件
│       └── Materials/
│           ├── _外链.md                # 外链记录（不下载）
│           └── week 1/lectures/Lecture_1.pdf
├── .crawl_status.json                  # 断点续爬状态（已 gitignore）
├── .content_snapshot.json              # 增量更新的比对基准（已 gitignore）
├── .crawl.lock                         # 单实例锁（已 gitignore）
├── reports/
│   ├── update_2026-01-01.md            # 每次增量运行的报告
│   └── latest-update.md                # 最新一份的快捷入口
├── configs/paths.json                  # 你的配置（已 gitignore）
├── configs/courses.json                # 不下载哪些课程（已 gitignore，见 --select）
└── .edge_profile/                      # 浏览器 profile（已 gitignore）
```

标记文件用中文命名（本项目起源于个人工具），都是普通 Markdown，忽略或改名都不影响：

| 文件 | 内容 |
|---|---|
| `_作业清单.md` | 每门课的作业索引：标题、路径、附件数 |
| `_未识别条目.md` | 既没有可下载附件、也没有正文的条目 —— **出现了一定要看** |
| `_外链.md` | 外链 URL，一行一个 |
| `<条目名>.md` | 正文型条目（没有附件的那种）的正文 |
| `<作业名>_作业要求.md` | 作业说明正文，在作业既有正文又有附件时才生成 |

## 学期映射

课程按其 BlackBoard 学期归入文件夹，解析优先级：

1. `configs/term_map.json` 的 `by_term_id`（精确匹配 `termId`）
2. `configs/term_map.json` 的 `by_term_name`（精确匹配学期名）
3. 内置自动规则，识别 `YY + T + 0 [+ PG|UG]` 形式的学期名：
   - `T=1` → `<YY>年下学期`
   - `T=2` → `<YY+1>年上学期`
   - `T=5` → `<YY+1>年夏季学期`
4. 都不匹配 → `未分类/`（dry-run 输出里看到它就该补映射了）

**内置自动规则编码的是某一所学校的命名习惯，大概率不适用于你的学校。**
如果 `--dry-run` 显示课程落到了 `未分类/`，在 `configs/term_map.json` 里手工补上 ——
手工映射永远优先。

## 实现说明

有意思的部分（以及踩过的坑）都写在
[docs/blackboard-structure.md](docs/blackboard-structure.md)：

- 如何通过 REST 接口枚举课程、遍历内容树
- 为什么课程左侧菜单不是内容区的可靠索引
- 如何还原真实的文件下载地址（接口里没有，只能从 HTML 包装页正则抠）
- 为什么文件名必须取自浏览器而非接口
- 在页面上下文里调接口所用的「fetch + 轮询」模式

## 已知限制

- **只支持 BlackBoard Classic**。Ultra 的 DOM 和接口完全不同。
- **学期自动映射是机构相关的** —— 见上。
- **新学期要补配置**。新学年意味着新的 term ID；`--dry-run` 出现 `未分类/` 就说明该补了。
- **全量模式（不带 `--update`）不做增量**。已完成的课整门跳过；增量用 `--update`，
  要重取用 `--force`（会删掉并重下整个课程文件夹）。
  唯一的例外是 `homework/` 子目录：它会被保留，因为默认假设放进那里的是你自己的东西、
  不是爬虫下的。如果你习惯用别的目录名放自己的文件，`--force` 之前先把那个名字加进
  `bb_crawler.py` 顶部的 `KEEP_ON_FORCE`。
- **限流要自己把握**。下载之间有短延时，但没有自适应退避。不要对没有授权的实例运行。

## 排错

| 现象 | 处理 |
|---|---|
| `缺少配置文件` | `cp configs/paths.example.json configs/paths.json` 并填写 |
| 卡在登录提示 | 在弹出的浏览器窗口里登录；太慢就调大 `login_timeout` |
| 课程落到 `未分类/` | 补 `configs/term_map.json` |
| `_未识别条目.md` 非空 | 这些条目既没有可下载附件、也没有正文。通常是老师建了还没填内容的空条目；有内容后这个文件会自己消失 |
| 某门课一直不下载 | 查 `configs/courses.json`，那是排除名单。`--dry-run` 会给这些行标上 `[已排除，不下载]` |
| `--select` 拒绝运行 | 它需要可交互的终端。改用 `--exclude <course_id>` 或直接编辑 `configs/courses.json` |
| 中文输出乱码 | 脚本已强制 UTF-8 stdout，检查终端编码 |
| 端口被占 | 脚本会杀掉占用该 profile 的进程并等端口释放 |

## 许可证

[MIT](LICENSE)

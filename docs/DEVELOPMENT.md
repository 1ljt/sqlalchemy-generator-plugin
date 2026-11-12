# 开发文档 — SQLAlchemy Generator Plugin

> 状态快照：2026-11-13 · 代码基线为 Phase 1（commit `3a9b433`）
>
> 本文档记录**已完成的工作、待完成的工作、已知问题**。架构规范与编码约束见
> [`.claude/skills/sqlalchemy-plugin-dev.md`](../.claude/skills/sqlalchemy-plugin-dev.md)，
> Python 引擎的接口契约见
> [`src/main/resources/python_engine/README.md`](../src/main/resources/python_engine/README.md)。

---

## 1. 当前状态速览

**一句话总结：算法密集的 Python 生成引擎已完成并通过验证；IDE 侧功能为零。**

| 维度 | 数据 |
|---|---|
| Python 引擎（不含测试） | **2624 行**，9 个模块 + 3 个模板 + CLI |
| Python 测试 | **3099 行**，359 个用例，全部通过 |
| 引擎行覆盖率 | **91.0%**（每个模块均 ≥80%） |
| Kotlin 代码 | **43 行**，其中 4 个文件是空占位类 |
| 插件可用性 | 沙盒 IDE 已确认能加载插件；Action 尚未实机点击验证（见 §4.1） |

| 阶段 | 状态 | Commit |
|---|---|---|
| Phase 0 · 项目基础整治 | ✅ 完成 | `4995641` |
| Phase 1 · Python 生成引擎 | ✅ 完成 | `3a9b433` |
| Phase 2 · Kotlin↔Python 通信层 | ⬜ 未开始 | — |
| Phase 3 · 连接 UI + 表选择 | ⬜ 未开始 | — |
| Phase 4 · 生成 + 写文件 | ⬜ 未开始 | — |
| Phase 5 · 模板/映射/持久化 | ⬜ 未开始 | — |
| Phase 6 · 测试/文档/发布 | ⬜ 未开始 | — |

按原始估算（总计 18 人日）已消耗 4 人日，**剩余约 14 人日**。
若砍掉 Phase 5（详见 §3.6），到"可用插件"约需 **8 人日**。

---

## 2. 已完成的工作

### 2.1 Phase 0 · 项目基础整治

- 删除全部模板占位代码（`MyBundle`、`MyToolWindowFactory`、`MyProjectService`、
  `MyProjectActivity`、`MyPluginTest` 及 `messages/`、`testData/`）。
- 建立五包结构：`actions` / `ui` / `engine` / `settings` / `model`。
- `plugin.xml` 重写：新描述、注册右键菜单 Action `SQLAlchemyGenerator.GenerateModels`、
  注册通知组 `SQLAlchemy Generator`。
- 移除 `testBundledPlugins("com.intellij.modules.ultimate")`，改为仅依赖
  `com.intellij.modules.platform`，满足 Community/Professional 全兼容要求。
- 新增依赖 `kotlinx-serialization-json:1.7.3`、`kotlinx-coroutines-core:1.9.0`
  与 `kotlin.plugin.serialization` 编译插件。
- `processResources` 排除 `python_engine/tests/**`、`tools/**`、`__pycache__`，
  已验证 jar 内只含引擎与模板。

**关键修复：包名不能以数字开头。** 原模板包名 `com.github.1ljt` 在 Kotlin 2.4.21
下无法编译（`1ljt` 不是合法标识符），**原模板本身就构建不过**。已全局改为
`com.github.ljt`，并同步 `plugin.xml` 的 `<id>`、`gradle.properties` 的 `group`、
源码目录结构。

### 2.2 Phase 1 · Python 生成引擎

位于 `src/main/resources/python_engine/`，纯 CLI、无状态、不写盘。

| 模块 | 职责 | 覆盖率 |
|---|---|---|
| `protocol.py` | Config/Result JSON 契约、方言默认端口、URL 构建与密码脱敏、退出码 | 96.8% |
| `dependencies.py` | 依赖检测 → 结构化错误 + 可复制的 pip 命令 | 87.5% |
| `type_mapper.py` | 数据库类型 → Python 注解 + SQLAlchemy 类型 | 89.2% |
| `naming.py` | 蛇形/帕斯卡转换、单复数、关键字转义、去重 | 85.1% |
| `schema.py` | 反射字典 → TableMeta，`mapped_column()` 参数渲染 | 92.7% |
| `relationships.py` | 外键 → `relationship()`，自引用与多路径消歧 | 93.8% |
| `inspector.py` | 真实反射，或离线 mock 元数据 | 81.2% |
| `sql_types.py` | SQL 类型字符串 → SQLAlchemy 类型（离线路径） | 82.6% |
| `renderer.py` | 导入头组装 + Jinja2 模块渲染 | 97.1% |

**已支持的三个 action**：`test_connection`、`list_tables`、`generate`。

**生成能力（全部已实现并测试）**：
- SQLAlchemy 2.0 风格：`DeclarativeBase` / `Mapped[T]` / `mapped_column()`，
  可空字段用 PEP 604 `str | None`，无 `Column()` 残留。
- 单文件与多文件两种输出模式；多文件模式生成蛇形命名文件 + `__init__.py` 导出。
- 外键自动推断双向 `relationship(back_populates=...)`；多文件模式用
  `TYPE_CHECKING` 块规避循环导入。
- 自引用外键（`remote_side`）、同一对表间多外键（`foreign_keys` 消歧）。
- 复合主键、多列唯一约束与索引 → `__table_args__`；schema → `{'schema': ...}`。
- `server_default=text(...)`、表注释 → 类 docstring、列注释 → `comment=`。
- Python 关键字列名转义（`class` → `class_` 并保留 `'class'` 列名）。
- MySQL/PostgreSQL 类型映射，含方言特有类型（`JSONB`、`HSTORE`、`INET`、
  `MONEY`、`BYTEA`、`ARRAY`、`TINYINT(1)`→bool 等）。
- 用户覆盖：`type_overrides`（表.列级）、`custom_type_map`（类型级）、
  `class_name_overrides`、`table_prefix_strip`、`template_dir`（自定义模板）。

**验证强度**：不止 `ast.parse()`。端到端测试会启动子进程跑 `generator.py`，
把生成文件写入临时包，真实 `import`，调用 `Base.registry.configure()`，
在 SQLite 建表、插入数据、双向遍历 relationship。

### 2.3 跨平台行尾修复

`core.autocrlf=true` 导致工作区 `gradlew` 变成 CRLF（Linux/macOS 上会
`bad interpreter: /bin/bash^M`），且打包的 Python 引擎与 Jinja2 模板也会被转成
CRLF——**而模板决定了所有生成文件的行尾**。已添加 `.gitattributes` 固定
（`.bat`/`.cmd` 保留 CRLF，jar/keystore/图片标记 binary）。索引本就是 LF，
未改动任何文件内容。commit `d686de8`。

---

## 3. 待完成的工作

### 3.1 Phase 2 · Kotlin↔Python 通信层（约 2 人日）

**交付物**
- `engine/PythonInterpreterResolver.kt` — 解释器发现：`PATH` 上的
  `python`/`python3`/`py`、Windows `py -0p` 列举、conda/pyenv 常见路径、
  项目内 venv（`.venv`/`venv`）。
- `engine/PythonEngineService.kt` — 项目级 Service：从 jar 释放
  `python_engine/` 到临时目录、`GeneralCommandLine` 调用、超时控制、
  stdout JSON 解析、stderr 收集、临时 config 用完即删。
- `engine/EngineResult.kt` + `model/*.kt` — kotlinx.serialization 数据类，
  与 `engine/protocol.py` 的 Config/Result 一一对应。
- `engine/PythonDependencyInstaller.kt` — 缺失依赖弹窗（包名、解释器路径、
  "复制命令"按钮）；**一键安装仅预留接口，不实现调用**。

**验收标准**
- 沙盒 IDE 中手动触发能成功调用引擎并解析结果。
- 未装 Python / 缺依赖 / 数据库连不上，分别得到明确且不同的提示，而非静默失败。
- 子进程环境变量含 `PYTHONUTF8=1` 与 `PYTHONIOENCODING=utf-8`。
- 临时 config 文件在进程退出后被删除；密码不出现在任何日志。
- JSON 协议正反序列化有单元测试。

**必须遵守的契约**（详见引擎 README §"Notes for the Kotlin side"）
- 大 schema 用 `--result-file` 而非读 stdout，避免 Windows 管道缓冲区死锁。
- 按退出码分支：`0` 成功 / `2` 缺依赖 / `3` 连不上 / `4` 配置错 / `5` 生成失败 / `1` 内部错误。
- Python 只返回 `relative_path` + `content`，**写文件由 Kotlin 负责**。

### 3.2 Phase 3 · 数据库连接 UI 与表选择（约 3 人日）

**交付物**
- `ui/DatabaseConnectionDialog.kt` — Kotlin UI DSL v2；方言切换、
  host/port/database/user/password、"测试连接"按钮（后台线程 + 即时反馈）。
- `ui/TableSelectionPanel.kt` — 表多选：schema 分组树、搜索过滤、
  全选/反选、列结构预览。
- `actions/GenerateModelAction.kt` — 替换占位实现，接入真实流程。

**验收标准**
- 右键 → "Generate SQLAlchemy Models" → 弹出连接配置窗。
- 填真实数据库信息能拉取并展示表列表，支持多选与搜索。
- 所有 DB 操作在后台线程（`Task.Backgroundable` 或 `Dispatchers.IO`），EDT 不冻结。
- 数百张表时 UI 不卡顿。

### 3.3 Phase 4 · 代码生成与文件写入（约 3 人日）

**交付物**
- `engine/CodeGenerator.kt` — 组装表选择 → 调引擎 → 解析文件列表。
- `engine/ModelFileWriter.kt` — `WriteCommandAction.runWriteCommandAction`
  + `VfsUtil.markDirtyAndRefresh`。
- `ui/GenerationOptionsPanel.kt` — 单/多文件、输出目录、基类名、
  前缀剥离、关系生成开关。

**验收标准**
- 端到端跑通：右键 → 连接 → 选表 → 选目录 → 生成 `.py`。
- 单文件模式所有模型在一个文件；多文件模式每表一文件 + `__init__.py`。
- 生成代码能被真实 Python 项目导入使用。
- 目标文件已存在时有覆盖确认；写入失败有回滚或明确报错。

### 3.4 Phase 5 · 高级功能（约 4 人日，可后置）

**交付物**
- `settings/PluginSettings.kt` — `PersistentStateComponent`：最近连接
  （密码走 `PasswordSafe`）、类型映射覆盖、模板偏好。
- `ui/TemplateEditorPanel.kt` — 内置 Jinja2 模板编辑 + 预览。
- `ui/FieldMappingEditor.kt` — 字段级映射覆盖 UI。

**验收标准**
- 重启 IDE 后连接配置与偏好保留；密码不以明文落盘。
- 可覆盖单列类型映射；可编辑模板并预览效果。

> **注**：引擎侧能力（`custom_type_map`、`type_overrides`、`template_dir`、
> `class_name_overrides`）**已全部实现并测试**。Phase 5 本质只是给既有能力套 UI，
> 因此可安全后置而不影响 Phase 2–4 的闭环。

### 3.5 Phase 6 · 测试、文档与发布（约 2 人日）

- Kotlin 端测试（当前 `test NO-SOURCE`），需先解决沙盒测试环境问题（见 §4.2）。
- 重写根 `README.md`（当前仍是模板样板文件，见 §4.6）。
- 更新 `CHANGELOG.md`、设置 Marketplace ID、插件签名与发布 Token。
- CI 验证 `./gradlew build` + `test`。
- 实机验证 PyCharm Community 与 IntelliJ IDEA Community。

### 3.6 建议的最短可用路径

**Phase 2 + 3 + 4（约 8 人日）** 即可得到完整闭环，Phase 5 后置。
代价是模板与字段映射只能手改配置文件，不能在 UI 里编辑。

---

## 4. 已知问题与风险

### 4.1 【已部分消除 · 中】插件在沙盒 IDE 中的运行验证

**2026-11-13 更新**：已执行过一次 `runIde`，沙盒 IDE 正常启动，且
`.intellijPlatform/sandbox/.../log_runIde/idea.log` 中确认：

```
Loaded custom plugins: SQLAlchemy Model Generator (0.0.1)
```

日志中**没有** ERROR 级记录（除 IDE 自带的 textmate bundle 噪音），也没有与
`com.github.ljt.*` 相关的异常。**插件能够被平台正确加载。**

**仍未验证的部分**：
- Action 是否在 `ProjectViewPopupMenu` 中正确显示并可点击。
  该次运行**没有打开任何项目**，而 `ProjectViewPopupMenu` 只在项目打开后
  的 Project 工具窗口中存在，因此右键菜单从未被触发过。
- 通知组 `SQLAlchemy Generator`（BALLOON）能否真正弹出气泡。

**验证方法**：在沙盒 IDE 中 New Project 建一个空项目 → `Alt+1` 打开 Project
工具窗口 → 右键任意文件 → 菜单**最底部**的 "Generate SQLAlchemy Models"。
或按 `Ctrl+Shift+A` 搜索该 Action 名。当前点击仅弹出一条占位通知，
不会连数据库或生成代码（那是 Phase 2–4）。

**另需注意**：该次 `runIde` 的沙盒是 `IU-2026.2.3`，即 **Ultimate**。
而本项目目标是 Community/Professional 全兼容（见 §7.3），因此
**Community 版下的加载与运行仍未验证**，Phase 6 必须补上。

### 4.2 【高】IntelliJ 测试沙盒存在内核组件实例化失败

Phase 0 曾写过一个最小的 `PluginLoadTest`，运行时沙盒报：

```
Cannot create extension (class=Z.Z.Z.Z.Z) [Plugin: com.intellij.modules.ultimate]
Caused by: java.lang.RuntimeException: Cannot find suitable constructor for class Z.Z.Z.Z.Z
```

这是 IntelliJ IDEA 2026.2.3 沙盒的内部问题，与本项目代码无关；但
`TestLoggerFactory` 会把任何 ERROR 级日志转成测试失败，导致测试无法通过。
当时的处理是**移除测试**，问题并未解决。

- **影响**：Phase 6 需要 Kotlin 端测试，届时必须正面解决。
- **可能方向**：改用 Community 版沙盒（`intellijIdea` 指定 IC 类型）、
  在测试中放宽 `TestLoggerFactory` 的严格度、或升级到修复该问题的平台版本。
- **附注**：`build.gradle.kts` 中 `intellijIdea(version)` 在
  IntelliJ Platform Gradle Plugin 2.19.0 下**没有** `type` 参数
  （尝试 `type = "IC"` 会编译失败），需查该版本正确的 IC 指定方式。

### 4.3 【高】Python 解释器发现逻辑未经验证

开发机是 Anaconda（`D:\develop\anaconda`），路径规整。真实用户可能是
Microsoft Store 版 Python、pyenv、多 conda 环境、或完全没配 PATH。

- **影响**：这是插件能否在他人机器上跑起来的第一道门槛。
- **建议**：Phase 2 中优先实现并手动验证多种布局；找不到时给出可操作的引导
  （而非仅报错），并允许用户在设置里手动指定解释器路径。

### 4.4 【中】PostgreSQL 路径完全未经真实验证

开发机未安装 `psycopg2`，因此：
- PostgreSQL 的类型映射只有**单元级**验证（构造 `postgresql.*` 类型对象），
  没有对真实 PG 库反射过。
- `test_inspector.py` 中 1 个用例被 skip（PG 引擎创建）。
- 真实反射路径是用**内存 SQLite** 代替验证的。SQLite 与 PG/MySQL 的反射
  行为存在差异（见 4.5）。

- **建议**：Phase 3 联调时务必接一个真实 PostgreSQL 实例回归。

### 4.5 【中】SQLite 反射不报告 autoincrement

`_is_autoincrement()` 依赖 `extra='auto_increment'`（MySQL）、`identity` 字典或
`nextval(...)` 默认值（PostgreSQL）。**SQLite 三者都不提供**，因此用 SQLite
反射出的整数主键不会生成 `autoincrement=True`。

- 这是**刻意为之**：猜测会给应用层赋值的整数主键错误地打上自增标记。
- 功能上无损——SQLAlchemy 对整数单列主键默认 `autoincrement="auto"`。
- 对 MySQL/PostgreSQL 这两个目标方言，检测逻辑正常，且已有单元测试覆盖。
- 已在 `tests/test_inspector.py::test_reflect_authors_columns` 中注明。

### 4.6 【中】根 README.md 仍是模板样板文件

内容还是 "Template ToDo list"、`MARKETPLACE_ID` 占位徽章、
"Fancy IntelliJ Platform Plugin" 之类。`CHANGELOG.md` 也只有模板初始条目。

- 插件 `plugin.xml` 里的描述已更新，但仓库门面对外仍是模板文案。

### 4.7 【中】Kotlin 侧存在 4 个空占位类

`ui/PluginUI.kt`、`engine/PluginEngine.kt`、`settings/PluginSettings.kt`、
`model/PluginModel.kt` 目前只是带文档注释的空类，用于在 git 中固定包结构。
Phase 2–5 实现时应删除或改写，不要遗留。

### 4.8 【低】单复数转换的固有局限

`naming.singularize()` 刻意保守，只处理无歧义后缀：
- `sses`/`xes`/`zes`/`ches`/`shes` → 去 `es`（`addresses`→`address`、`boxes`→`box`）
- 辅音 + `ies` → `y`（`categories`→`category`）
- 其余 `s` 结尾（非 `ss`/`us`/`is`）→ 去 `s`
- `status`/`news`/`series`/`species`/`media` 等不变词受保护

已知漏判：`buses` → `buse`（应为 `bus`）。此类词在数据库表名中罕见。
用户可用 `class_name_overrides` 逐表修正，或关闭 `singularize_class_names`。

### 4.9 【低】连续缩写词无法可靠切分

`to_snake_case("parseHTTPURL")` → `parse_httpurl`（期望 `parse_http_url`）。
`HTTPURL` 内部没有大小写跳变，任何算法都无法区分 `HTTP+URL` 与 `HTTPU+RL`。
常见情形（`HTTPResponse`→`http_response`、`userID`→`user_id`）正常。
已在测试中记录为已知限制。

### 4.10 【低】复合外键不生成 relationship

多列外键需要手写 `primaryjoin`，自动生成容易静默出错，因此**刻意跳过**，
只保留 `ForeignKey(...)` 列定义。已有测试固化该行为。

### 4.11 【低】枚举默认降级为 String

`enum_as_native` 默认 `false`，MySQL/PG 的 ENUM 生成 `String(n)`
（n = 最长枚举值长度），丢失取值集合信息。这是为规避迁移工具处理
PG enum 类型的麻烦而做的权衡。设 `enum_as_native: true` 可生成
`Enum('a','b', name='...')`。

---

## 5. 环境与工具链

| 组件 | 版本 |
|---|---|
| Gradle | 9.8.1（wrapper） |
| Kotlin | 2.4.21 |
| IntelliJ Platform Gradle Plugin | 2.19.0 |
| Changelog Plugin | 2.5.0 |
| 目标 IDE | IntelliJ IDEA 2026.2.3 |
| kotlinx-serialization-json | 1.7.3 |
| kotlinx-coroutines-core | 1.9.0 |
| JUnit | 4.13.2 + Platform TestFramework |
| Python（开发机） | 3.13.5（Anaconda） |
| SQLAlchemy / Jinja2 | 2.0.52 / 3.1.6 |
| pymysql | 已安装 |
| psycopg2 | **未安装** |
| pytest / coverage | **未安装**（测试用 stdlib `unittest`） |

依赖 `com.intellij.modules.platform`，兼容 IDEA/PyCharm 的 Community 与
Professional/Ultimate。**未引入任何 JDBC 驱动**——所有数据库操作由 Python
引擎经 SQLAlchemy 完成。

---

## 6. 开发命令速查

```bash
# Kotlin 插件
./gradlew build                    # 编译 + 打包
./gradlew runIde                   # 启动沙盒 IDE（尚未验证，见 §4.1）
./gradlew clean

# Python 引擎（无需第三方 runner）
cd src/main/resources/python_engine
python -m unittest discover -s tests -t .     # 359 个用例
python tools/measure_coverage.py              # 覆盖率，阈值 80%，低于则退出码 1

# 手动跑一次生成（离线，不需要数据库）
python generator.py --config <config.json> --pretty
```

覆盖率测量**不要**用 stdlib 的 `trace` 模块：其 `_Ignore.names()` 按裸模块名
缓存忽略决定，会整文件漏记（`engine/schema.py` 曾测出 0%，而实际有 34 个
通过的测试）。用 `tools/measure_coverage.py`。

---

## 7. 架构决策记录

### 7.1 数据库连接统一由 Python 引擎处理
Kotlin 端不引入任何 JDBC 驱动，"测试连接"也是调 Python 执行
`engine.connect()`。**理由**：保持插件轻量，避免打包与分发数据库驱动。

### 7.2 Python 依赖采用"明确提示 + 可选一键安装"
引擎启动时检测依赖，缺失则返回结构化错误（含 `missing`、
`missing_distributions`、`install_command`、`interpreter`）。Kotlin 端弹窗
展示并提供"复制命令"按钮。**一键安装仅预留接口，初版不实现调用**。
**理由**：不擅自修改用户 Python 环境。

### 7.3 Community + Professional 全兼容
仅依赖 `com.intellij.modules.platform`，禁止 `com.intellij.database`、
`com.intellij.modules.ultimate`、`com.intellij.modules.python`。

### 7.4 Kotlin 包名使用 `com.github.ljt`
原 `com.github.1ljt` 在 Kotlin 2.4+ 下无法编译（见 §2.1）。

### 7.5 文件写入由 Kotlin 负责
Python 只返回内容，不写盘。**理由**：保持 IDE 撤销栈与 VFS 一致，
且便于写入前预览。

### 7.6 生成代码的默认风格取舍
| 选项 | 默认 | 理由 |
|---|---|---|
| `singularize_class_names` | `true` | `users`→`User` 符合普遍预期 |
| `enum_as_native` | `false` | 规避迁移工具处理 PG enum 的麻烦 |
| `use_dialect_types` | `false` | 生成代码不绑定具体方言 |
| `escape_builtin_names` | `false` | `id`/`type` 作列名极常见，转义反而意外 |
| `use_future_annotations` | `true` | 支持 relationship 的前向引用 |

以上均可通过 config 覆盖。

### 7.7 类型映射按 `(__visit_name__, dialect)` 查表，不用 `isinstance`
`mysql.LONGTEXT` 继承自 `sqlalchemy.String` 而非 `Text`；`mysql.BIT` 与
`postgresql.BIT` 同名不同义。类层次单独判断会误映射真实 schema。
需注意 `__visit_name__` 的两个陷阱：`BigInteger` 报 `BIG_INTEGER`
（`BIGINT` 是 SQL 别名）；所有 `TypeDecorator`（含 `Interval`）都报无区分度的
`TYPE_DECORATOR`，需类 MRO 兜底 + `.impl` 解包。

---
name: sqlalchemy-plugin-dev
description: IntelliJ IDEA / PyCharm 插件开发与 SQLAlchemy 2.0 代码生成引擎架构规范
---

# SQLAlchemy 2.0 Generator Plugin 开发技能规范

你正在开发一个面向 IntelliJ IDEA 与 PyCharm 的插件，用于根据现有数据库表结构批量生成符合 SQLAlchemy 2.0 规范的 Python ORM 代码。

## 1. 核心架构与职责边界
本项目采用 **“Kotlin UI 壳 + 内置 Python 生成引擎”** 的混合架构，严格遵守以下职责分离：
- **Kotlin 插件端 (`src/main/kotlin/`)**：
  - 仅负责 IDE 交互（右键菜单 Action、ToolWindow、配置弹窗 Dialog）。
  - 负责读取用户配置的数据库连接参数、展示表列表多选框、选择输出目录。
  - 负责安全调用宿主环境的 Python 解释器执行生成脚本，并使用 IntelliJ `VirtualFile` / `WriteCommandAction` 写入最终生成的代码文件。
  - **禁止**在 Kotlin 中手写复杂的 SQL 方言解析或 SQLAlchemy 类型映射逻辑。
  - **禁止**引入 JDBC 驱动依赖或任何数据库连接库——**所有数据库操作（包括连接测试、元数据提取）统一由 Python 引擎通过 SQLAlchemy 完成**，Kotlin 端仅传递用户填写的连接参数（host、port、user、password、database），保持插件轻量、无数据库驱动依赖。
- **Python 生成引擎 (`src/main/resources/python_engine/`)**：
  - 负责通过 `sqlalchemy.inspect` 连接 MySQL / PostgreSQL 并提取表、列、主键、外键、索引及注释元数据。
  - 负责**测试连接**（`engine.connect()` 验证连接可达性），将结果通过标准化 JSON 协议返回 Kotlin 端。
  - 负责将数据库类型映射为 Python 类型注解，并通过 Jinja2 模板渲染代码。
  - 必须保持无状态、纯 CLI 驱动，通过标准 JSON 输入输出与 Kotlin 通信。

## 2. SQLAlchemy 2.0 代码生成硬性规范
所有生成的 Python ORM 模型必须严格遵循 SQLAlchemy 2.0 现代风格，**严禁生成 1.x 旧版 `Column()` 写法**：
- **导入规范**：必须使用 `from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship`。
- **字段声明**：
  - 非空字段：`name: Mapped[str] = mapped_column(String(50), nullable=False)`
  - 可空字段（使用 Python 3.10+ 联合类型语法）：`email: Mapped[str | None] = mapped_column(String(100))`
  - 主键自增：`id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)`
- **关联关系**：根据外键自动推断 `relationship(back_populates="...")`，注意处理双向引用与循环导入（必要时使用 `TYPE_CHECKING` 块）。
- **文件拆分模式**：
  - `single` 模式：所有表合并输出到一个指定 `.py` 文件。
  - `multiple` 模式：每张表输出独立的蛇形命名文件（如 `user_profile.py`），并同步生成或更新同级目录下的 `__init__.py` 导出所有模型类。

## 3. Kotlin 插件开发规范
- **UI 构建**：一律使用 **JetBrains Kotlin UI DSL v2**（`com.intellij.ui.dsl.builder.panel`）构建对话框和设置面板，禁止手写原始 Swing 布局管理器（如 `GridBagLayout`）。
- **线程模型（极重要）**：
  - 任何数据库连接测试、表结构拉取、Python 子进程执行，**必须**放在后台线程（`ProgressManager` / `Task.Backgroundable` 或协程 `Dispatchers.IO`）中执行，严禁阻塞 EDT（UI 线程）。
  - 写入或修改项目文件时，**必须**包裹在 `WriteCommandAction.runWriteCommandAction(project) { ... }` 中，并在写入后调用 `VfsUtil.markDirtyAndRefresh` 刷新文件树。
- **兼容性（强制约束）**：
  - 插件必须兼容 **IntelliJ IDEA Community / Ultimate** 与 **PyCharm Community / Professional**。
  - 仅依赖 `com.intellij.modules.platform` 等基础通用模块，**严禁**依赖 `com.intellij.database`、`com.intellij.modules.ultimate`、`com.intellij.modules.python` 等闭源或专业版专属 API。
  - Gradle 配置中使用 `intellijIdea("community")` 或确保 `testBundledPlugins` 不引入 Ultimate 专属模块。

## 4. Kotlin 与 Python 进程通信协议
- Kotlin 释放 `python_engine` 到系统临时目录后，通过 `GeneralCommandLine`（IntelliJ SDK 自带，优于原生 `ProcessBuilder`）调用 Python 解释器。
- **传参方式**：为避免命令行长度限制及密码在进程列表中泄露，Kotlin 应将配置写入临时 JSON 文件（如 `config.json`），执行：
  `python generator.py --config /path/to/config.json`
- **输出规范**：
  - Python 脚本的 `stdout` **只能**输出结构化的 JSON 结果（如 `{"status": "success", "files": [...]}` 或 `{"status": "error", "message": "..."}`）。
  - 所有调试日志必须输出到 `stderr`，防止污染 JSON 解析。
  - 大 schema 优先使用 `--result-file <path>`，避免 Windows 管道缓冲区上限导致死锁。
- **退出码约定**（Kotlin 端据此分支处理，详见 `engine/protocol.py`）：

  | 退出码 | 含义 | `error_type` |
  |---|---|---|
  | 0 | 成功 | — |
  | 1 | 未预期的内部错误 | `internal_error` |
  | 2 | 缺少 Python 依赖 | `missing_dependencies` |
  | 3 | 数据库连不上 | `connection_failed` |
  | 4 | 配置无效/不完整 | `invalid_config` |
  | 5 | 生成过程失败 | `generation_failed` |

- **编码（Windows 必查）**：`GeneralCommandLine` 必须为子进程注入环境变量 `PYTHONUTF8=1` 与 `PYTHONIOENCODING=utf-8`。否则 Windows 子进程回退到控制台代码页（GBK），含中文注释的表结构在 stderr/traceback 中会解码失败。引擎侧 stdout 默认 ASCII 转义（`\uXXXX`），保证任何读取端编码都不会损坏数据。
- **文件写入职责**：Python 只返回 `relative_path` + `content`，**绝不自己写盘**；由 Kotlin 在 `WriteCommandAction` 中写入并 `VfsUtil.markDirtyAndRefresh`，以保持 IDE 撤销栈与 VFS 一致。
- **临时文件安全**：`config.json` 含明文密码，进程退出后必须立即删除；结果 JSON 中的密码字段已由引擎脱敏为 `***`，可安全写入日志。

## 5. Python 依赖管理策略

采用 **"明确提示 + 可选一键安装"** 策略，不隐式修改用户环境：

- **依赖检测**：Python 引擎启动时检测必需依赖（`sqlalchemy`、`jinja2`、`pymysql`/`psycopg2`/`asyncpg`），缺失时通过 stderr 输出缺失包列表并退出（exit code 非零）。
- **Kotlin 端响应**：
  - 解析 Python 引擎返回的结构化错误 JSON（含 `missing_dependencies` 字段与当前解释器路径）。
  - 弹窗显示缺失包名、解释器路径和完整的 `pip install` 命令。
  - 提供 **"复制命令"** 按钮，将 `pip install sqlalchemy jinja2 pymysql ...` 复制到剪贴板。
  - 预留 **"一键安装"** 按钮接口（`PythonDependencyInstaller` 服务，初版仅定义接口并注释掉调用，后续版本通过 `GeneralCommandLine` 执行 `pip install` 实现自动安装）。
- **原则**：不擅自执行 `pip install`，不修改用户 Python 环境的 site-packages，让用户完全掌控依赖安装时机和范围。

## 6. 开发与修改工作流
1. 修改 Python 生成逻辑时，在 `src/main/resources/python_engine/tests/` 下补充测试用例。测试用 **`unittest`** 编写（stdlib 即可运行，`pytest` 也能直接收集，不强制安装第三方 runner）：
   ```bash
   cd src/main/resources/python_engine
   python -m unittest discover -s tests -t .
   python tools/measure_coverage.py    # 纯 stdlib 行覆盖率，阈值 80%
   ```
2. **`ast.parse()` 不足以验证生成代码**。它只能证明语法合法，无法发现 SQLAlchemy 在类定义期就会抛出的错误。典型反例：`mapped_column(String(50), 'userName')` 语法完全正确，但列名必须在类型**之前**，否则字符串会落进 `*arg`（期望 `SchemaItem`）而抛 `ArgumentError`。因此端到端测试必须真正 **导入生成的模块 → `Base.registry.configure()` → 建表 → 插入数据 → 遍历 relationship**。
3. 覆盖率测量不要用 stdlib 的 `trace` 模块：其 `_Ignore.names()` 按裸模块名缓存忽略决定，会整文件漏记（`engine/schema.py` 曾测出 0% 而实际有 34 个通过的测试）。用 `tools/measure_coverage.py`。
4. 修改 Kotlin 代码后，必须检查 `plugin.xml` 中的 Action ID、依赖声明与类路径是否完全匹配。
5. 每次新增用户可配置项（如自定义类型映射规则、基类导入路径），必须同步更新四处：UI 面板状态持久化（`PersistentStateComponent`）→ JSON 通信结构（`engine/protocol.py` 的 `GenerationConfig`）→ 引擎逻辑 → Jinja2 模板。
6. **Kotlin 包名不能以数字开头**：Kotlin 2.4+ 编译器拒绝 `com.github.1ljt` 这类包名段（原模板即因此无法编译）。包名统一使用 `com.github.ljt.*`；`plugin.xml` 的 `<id>` 与 `gradle.properties` 的 `group` 必须同步。
7. 类型映射不要依赖 `isinstance` 判断：`mysql.LONGTEXT` 继承自 `sqlalchemy.String` 而非 `Text`，且 `mysql.BIT` 与 `postgresql.BIT` 同名不同义。引擎按 `(__visit_name__, dialect)` 查表；注意 `BigInteger` 的 visit_name 是 `BIG_INTEGER`（`BIGINT` 是 SQL 别名），而所有 `TypeDecorator`（含 `Interval`）都报无区分度的 `TYPE_DECORATOR`，需靠类 MRO 兜底。
# CCM Architecture Baseline

本文定义重构的目标架构与约束，不表示现有代码已经全部实现。阶段范围和验收见 [重构实施计划](docs/refactoring-plan.md)。

## 当前前提

CCM 尚未正式使用，无需要保留的历史用户数据，无旧 API、旧数据库或旧配置兼容包袱。重构可以直接删除错误模型与接口，不为已删除功能保留兼容层、影子表或双写路径。现有财务语义、有效解析能力和安全边界仍须保留；无兼容包袱不等于可以无意损失功能。

此声明不授权删除任何现存容器、数据目录、凭据或备份。部署、重置环境和发布属于另行明确范围的操作。

## 七条硬原则

1. **Single-owner local-first**：一个实例一个 Owner，自托管，不依赖 CCM 中心服务。未来家庭多人优先独立容器与数据卷，不引入多租户数据库。
2. **Modular Monolith**：生产为一个 Docker 容器、一个 FastAPI/Uvicorn 应用进程与一个 worker、一个 SQLite 数据库。模块化代码，不拆服务；rclone 等按需工具子进程不属于新增服务。
3. **Facts by lifecycle**：用户事实、公共事实、可执行参考规则与敏感凭据分别存放，禁止把公共参考默认值自动写成用户事实。
4. **One ingestion path**：受支持的 IMAP、EML、PDF、上传输入最终进入同一个导入用例。Parser 不访问数据库，IMAP 不拥有账单业务逻辑。
5. **Backend is domain authority**：WebUI、未来 App 和 Agent 都是 API 客户端；财务语义、权限与业务规则由后端负责。
6. **Thin integrations**：IMAP、通知渠道、rclone 负责 I/O，不拥有账户、账单或到期日规则。
7. **Complexity must pay rent**：新增表、状态、服务、后台任务或通用抽象，必须证明真实需求、隔离需求、可测试性或变化边界的收益。

## 产品与部署边界

- 不做 SaaS、用户注册、中心账号、云端托管、共享数据库多租户或 RBAC 平台。
- Linux Docker 是正式目标；Windows 不作为 release gate。
- 每个实例独立 SQLite、Secrets、IMAP 配置、Agent token、通知、备份目的路径和字典 override。
- 同一主机的多个容器是实例级隔离，不是物理主机隔离。部署多个实例时应避免共享数据卷与凭据。
- 浏览器 Cookie 不按端口隔离；多实例应使用独立主机名与 host-only Cookie，或明确的实例级 Cookie 隔离配置。
- 核心与 WebUI 可离线使用；IMAP、远程通知和远程备份等外部连接由用户配置，不承诺这些集成离线工作。

## 数据生命周期

| 内容 | 唯一权威位置 |
| --- | --- |
| 账户、卡、Statement、Transaction、Repayment、ImportRecord | SQLite |
| Owner Session、IMAP 状态、用户设置、通知记录、备份设置 | SQLite |
| Manual Bill、可选功能的用户数据 | SQLite |
| 银行资料、卡产品、标准年费、长期权益、官方来源 | `dictionary.json` |
| 账单日边界、到期日参考规则、产品例外 | `bank_billing_rules.json` |
| IMAP、通知等敏感凭据 | `/data/config/secrets.json`，不含需单独明确的启动凭据 |
| 日志、备份产物 | 文件系统 |

引用产品 ID、记录用户选择和银行原始账单事实，不等于复制字典。禁止的是把参考默认值伪装为用户确认或真实观测值。

### Dictionary

- Git 维护 `dictionary.json`，镜像携带 bundled 离线副本。
- 可选人工覆盖文件为 `/data/dictionary/dictionary.json`。
- 启动时校验并加载一次；合法 override 整份替换 bundled，不做隐式合并。
- override 不存在时使用 bundled；无效或不可读时明确警告并回退 bundled。bundled 无效时启动失败。
- 加载结果只读；运行时不下载、不扫描、不审批、不更新、不写 SQLite、不 hot reload。
- 更新方式为人工替换 override 后重启容器。
- 校验格式版本、稳定银行/产品 ID、引用关系、金额与币种等；提供来源、版本和内容摘要用于诊断，不暴露凭据。
- 删除 Runtime Catalog 及其八张表、扫描/审批/同步接口与管理 UI。只读查询由内存字典提供；不保留维护功能的兼容壳。
- 保留有用的银行和产品查询能力，但旧响应字段、路由名称和数据库生成 ID 不构成兼容承诺。
- 字典与 billing rules 永久分开；产品资料缺失不能阻止已持有卡与历史账单的正常读取。

## 财务不变量与领域边界

- 金额使用整数 minor units，并带明确币种；禁止无明确汇率与口径的跨币种业务汇总。
- `Statement.total_due` 是银行账单事实；交易合计只用于分析、统计与核验，不得反向覆盖它。
- 长期模型为 BillingAccount → Card，以及 BillingAccount → Statement → Transaction；Repayment 属于 Statement。
- Statement 必须有账户归属；Transaction 可不识别具体卡，但若指定 Card，其归属必须与账单账户语义一致。
- BillingAccount 单独设计评审，先解决账户身份、多币种与额度口径、换卡/销卡、同尾号及账户归属证据；不能只把现有 Card 字段机械搬过去。
- 不能仅凭银行或尾号自动猜测账户；导入时归属有歧义要明确拒绝或要求人工选择。
- 实际账单的 due date 优先；预测由 `due_date_rule_resolver` 统一使用历史证据、用户确认规则和公共 fallback。证据冲突需显式处理，不能静默覆盖用户设置。
- Dictionary 不推导用户实际到期日，前端不重复实现到期日规则。

### 年费

- 字典保存标准年费和官方政策；交易保存真实扣费；用户可明确设置手动覆盖。
- 展示选择为 manual > observed > dictionary reference，同时返回来源、币种和必要的观测时间。
- 参考费率、真实扣费和预计负担是不同口径；不得将标准年费直接算作实际支出，也不能把任意一次历史扣费永久当作当前费率。
- effective annual fee 是 read model，不写回 SQLite，不建立额外年费状态机或观察表。
- 从字典解耦阶段开始停止所有字典自动写回，包括前端自动填入后冒充手动值的路径。

### 导入

统一路径：Source Adapter → ParserRegistry → ParsedBillingDocument → Resolve/Validate → Import Use Case → Persistence。

- bean-sieve 保留为 Parser helper，删除 ParsedBatch/ParsedEntry 第二套持久化模型。
- 删除旧路径前盘点其独有输入能力。只有明细、没有正式摘要的输入不能通过交易求和伪造 Statement；必须给出明确结果。
- 成功导入的 ImportRecord、Statement、Transactions 和关系在一个业务事务中提交。
- 业务失败全部回滚，之后以独立短事务记录失败审计；审计失败也不能伪报导入成功。
- 幂等、去重、失败重试与并发重复导入必须保持明确语义；失败审计不能阻止合法重试。

## WebUI 与 API

- Vue 3 + Vite + SFC，本地依赖、本地 ECharts、构建期 Tailwind。TypeScript 可以逐模块采用，不作为首次迁移前置条件。
- 前端分为 `api/`、`lib/`、`components/`、`composables/` 和按业务划分的 `features/`；App 只承担布局和导航。
- HTTP 鉴权、CSRF、错误处理归入统一 API client；金额和日期展示使用明确的公共工具。
- 前端允许格式化、百分比、排序、筛选和图表转换；禁止自定义还款状态、年费来源、到期日规则和跨币种业务金额合计。
- 不把巨型 HTML 换成巨型 composable 或共享全局状态对象；每个 feature 拥有自己的状态，跨模块依赖显式暴露。
- Node 仅用于 Docker 构建阶段；生产镜像只包含 Python 应用及静态 dist，不运行 Node/npm/Vite server。
- 不引入 SSR、Nuxt、复杂路由或全局状态框架，除非真实需求经评审成立。

## 安全、后台任务与基础设施

- Browser：Owner Token 登录 → Owner Session + CSRF。
- Agent：独立 Bearer Token + exact route allowlist，默认拒绝未授权路由。
- 移除旧管理员 token 模式时同步移除配置、文档、测试和调用端；不保留兼容模式。开发 bypass 只能在非生产使用。
- `main.py` 为 composition root；三个 asyncio 后台循环（IMAP、提醒、备份）的启停与取消收敛到 `runtime/background.py`，不引入调度框架。
- 保留 SQLite、SQLAlchemy、Alembic、foreign keys、busy timeout。WAL 暂不启用；需要真实锁竞争证据及备份恢复验证后再评估。
- 保留 SQLite 一致性备份 → staging → rclone。数据库备份不等于完整实例备份，必须说明 Secrets、override 和镜像版本的恢复方式，不擅自上传 Secrets。
- 通知渠道只接收已生成的通知内容，不直接推导账单业务规则。
- Manual Bills、Mortgage、Installment、Interest-free 为可选功能；可以提供 dashboard/calendar/analytics 数据，不能改变信用卡核心模型。

## Schema、验证与变更管理

- 本轮无历史数据升级要求。允许按阶段调整未发布 schema，必要时重建或 squash 初始 Alembic 基线；不为删除功能新增只服务旧库的迁移链。
- 每次 schema 改动仍必须验证全新空库初始化、ORM/schema 一致性、约束与财务不变量；不得把“没有历史数据”当成跳过数据库验证的理由。
- 不自动删除或重置现有数据库。正式发布前确定并冻结初始 migration 基线，此后使用向前迁移。
- 每阶段独立 PR、独立验收、可停止；在自己的隔离临时环境验证，不修改共享实例。
- 测试与构建在 PC/原生 Linux 文件系统或 CI 执行；NAS 仅做轻量静态检查，未经当前任务明确授权不得转在 NAS 测试。
- 测试串行调度，runner 内部最多两个 worker；保留原有串行通道，失败、超时或 OOM 后停止，不自动重跑。
- PR 必须说明所属阶段、范围/非目标、影响的不变量、执行过与未执行的验证、已知限制。删掉过时实现测试时应补上新边界测试，不能通过删除有效测试掩盖功能退化。

## 明确不做

没有经评审的真实需求，不引入 PostgreSQL、Redis、Celery、消息队列、事件总线、微服务、generic repository、DI 框架、CMS、运行时字典更新器、审批工作流、通用 scheduler、多租户 users/tenant_id/RBAC。

后端按业务边界渐进模块化，不为目录美观一次性搬迁所有文件。需要偏离本基线时，先说明需求、替代方案、成本与影响的不变量，再更新决策；不通过实现细节偷偷扩大产品范围。

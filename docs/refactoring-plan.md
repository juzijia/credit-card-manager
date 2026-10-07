# CCM 分阶段重构计划

上位约束：[ARCHITECTURE_BASELINE.md](../ARCHITECTURE_BASELINE.md)。本文是实施计划，不是完成报告。

## 执行前提

- 尚未正式使用，无历史用户数据、旧库升级和旧接口兼容要求。
- 不再预定 `0003_remove_runtime_catalog` 等兼容迁移编号；未发布 schema 可直接修正，必要时重建初始 Alembic 基线。
- 前后端、Agent 文档、测试必须随接口变更同阶段更新；不为仓库内部调用方留下断裂接口。
- 每阶段独立分支/PR；不批量搬目录、不引入双写、不修改既有容器和数据目录。
- 本计划不授权所有阶段自动连续执行，不授权部署或重置环境。

## 阶段总览

| 阶段 | 范围 | 完成条件 |
| --- | --- | --- |
| Phase 1 | 静态 Dictionary、删除 Catalog Runtime、停止字典写回 | 启动只读加载，八张 Catalog 表与维护入口消失，用户字段不被字典自动填充 |
| Phase 2 | Frontend U0/U1：Vite、本地资源、API/money/date | 无运行时 CDN；HTTP 层集中；生产镜像不需要 Node；不同时重写所有页面 |
| Phase 3 | 统一 Import Pipeline、删除 ParsedBatch/ParsedEntry | 支持范围明确，所有入账走同一用例，事实原子提交、失败审计独立记录 |
| Phase 4 | 年费 read model | 明确参考、实际扣费与手动值，返回来源，不持久化 effective 值；无需历史清理脚本 |
| Phase 5 | Frontend feature/component split | 各 feature 拥有状态；页面/弹窗/通用组件边界明确，不产生新的巨型共享模块 |
| Phase 6 | Auth 简化、main/background 分离 | Owner/Agent 两种生产身份，默认拒绝；后台任务可正确启停，无新调度框架 |
| Phase 7 | BillingAccount/Card 独立设计与实现 | 先评审身份/币种/额度/生命周期，再在全新库验证账户与财务关系；无旧数据猜测迁移 |

## Phase 1：Dictionary storage decoupling

### 目标与非目标

恢复最初的静态字典边界，不把 Catalog 数据库换成另一个运行时内容系统。

本阶段不做 BillingAccount、统一导入、认证模式重构、Vite 迁移或全面重排目录；只删除前端 Catalog 维护界面并修改相关读取/提交逻辑。保持 billing rules 与 due resolver 的现有业务语义。

### 1. 静态资产和模块

建议模块为 `server/app/modules/dictionary/`，直接实现校验、启动加载和只读查询，不增加 repository 框架或 DI 容器。

- 将 `services/catalog_service.py` 中的银行公共注册资料与 `data/catalog_fixtures.json` 的产品资料收敛到 `server/app/data/dictionary.json`。
- 文件具有 `schema_version`，银行与产品显式稳定 ID，以及年费的金额单位/币种语义。具体字段在实现前形成 DTO/schema 与示例。
- 完整保留有效银行服务资料、产品别名、卡面、年费政策、权益和来源；不把 scan、approval 或数据库生命周期字段搬进文件。
- 数据转换提供机械核对：银行/产品数量、稳定 ID 唯一性、权益/来源/卡面数量及关键字段差异。原始缺失值不得用猜测补齐。
- `bank_billing_rules.json` 不合并、不替换、不参与用户字典覆盖。
- 明确本地卡面引用的静态资源根目录，拒绝越界路径；外链只作引用，不在启动时下载。外部图片不可用时有本地占位，不阻塞核心 UI。

### 2. 加载契约

| 输入 | 结果 |
| --- | --- |
| 无 override | 加载 bundled |
| 有效 override | 整份使用 override |
| override 语法、版本、引用或金额校验失败 | 警告并使用 bundled，不部分加载 |
| override 不可读 | 警告并使用 bundled |
| bundled 校验失败 | 明确启动失败 |
| 运行中替换文件 | 当前实例不变，重启才生效 |

- override 固定路径 `/data/dictionary/dictionary.json`，bundled 为镜像内资产。
- 启动建立不可变快照，查询不能修改快照，也不能每次请求重新读文件。
- 设置合理文件大小限制；拒绝重复 JSON 键、重复实体 ID、未知 schema 版本和悬空引用。
- 日志或只读诊断响应提供所选来源、版本、内容摘要及回退原因；不另建后台任务。
- 纯字典查询逻辑不依赖 SQLAlchemy Session；全局认证仍可正常使用 Owner Session 数据库，这不算字典耦合。

### 3. 删除范围

删除下列 ORM 模型及空库 schema 中对应表：

```text
bank_registries
catalog_products
catalog_artworks
catalog_benefits
catalog_sources
catalog_scan_runs
catalog_scan_items
dictionary_change_requests
```

删除或重写全部引用方，包括：

- `services/catalog_scanner.py`、`services/catalog_diff.py`。
- `services/catalog_service.py` 中 DB seed、upsert、scan、approval 等能力；公共资料转入静态资产后不保留重复常量源。
- `api/v1/catalog.py` 的 ingest/sync/scan/change-request/approve/reject 等维护能力及对应 schema。
- `main.py` 的 Catalog 启动 seed/refresh。
- `models/__init__.py`、Alembic metadata/schema 与相关外键或测试 fixture。
- 安全 allowlist、Agent skill/API 文档、README 中的已删除能力。
- WebUI 的扫描、审批、同步维护入口及对应状态、请求、帮助文本；确认无其他用途后删除 `web/js/card_dictionary.js`。
- 所有远程字典下载配置与代码；不因保留其他网络集成而误删 IMAP、通知或 rclone。

默认继续使用 GET `/catalog/stats`、`/catalog/banks`、`/catalog/products` 承载有用的只读能力，避免无收益的路径改名；这只是当前实现选择，不是兼容承诺。移除扫描/审批统计字段，必要时调整产品响应并同步修改调用方，不伪造旧数据库 ID 或维护状态。

### 4. 同阶段切断字典写回

- 移除 `apply_product_dictionary`、`refresh_dictionary_cards` 及创建/更新/导入时的自动赋值调用。
- 产品匹配改为只读静态查询；歧义返回明确未匹配，不任取一项。
- 前端选择产品时只选择产品引用，不把参考年费/政策注入手动设置 payload。
- 标准年费仍可通过产品详情展示，明确标识“参考”；手动输入是独立、显式的用户行为。
- API 不允许客户端通过 `annual_fee_source=dictionary` 等字段重新建立自动来源写回路径；由后端定义可写字段与来源语义。
- 不引入历史数据清理脚本；当前无此需求。Phase 4 再完善统一的 effective annual fee 展示和统计口径。

### 5. Phase 1 验收清单

以下均为待执行验收，不代表已经通过。

- [ ] 字典 schema 与稳定 ID 明确；内容转换差异可审阅，公共资料无意外丢失。
- [ ] 加载契约所有分支覆盖，包括回退、无网络启动与运行期不 reload。
- [ ] 查询只依赖只读字典快照；排序/过滤/缺失引用行为明确，响应无法改变快照。
- [ ] 全新空库初始化成功，不包含上述八张表；ORM metadata 与实际 schema 一致。
- [ ] 不存在 Catalog seed、远程下载、scan、approve、同步任务或维护路由。
- [ ] 创建/修改卡与导入不会自动复制字典年费；字典替换不会改动用户持久化字段。
- [ ] 前端查看银行/产品/卡面正常；维护 UI 移除；选择产品的请求不伪装手动年费。
- [ ] billing rules、due resolver、Statement 权威值、交易和还款语义未改变。
- [ ] Owner/Agent 的现有鉴权和只读权限有效；删除路由后 allowlist 不残留维护授权。
- [ ] 文档说明人工 override + 重启，不再宣称运行时下载、扫描或同步。
- [ ] 适用验证记录齐全，未执行项与阻塞明确，未操作共享数据或运行实例。

### 6. 实施与提交顺序

1. 定义字典文件 schema，转换公共资料并核对内容。
2. 实现只读模块与加载分支，补对应测试。
3. 接入 API 与产品查询，同步停止前后端自动写回。
4. 删除 Catalog Runtime、八张表的定义、旧 UI 和废弃依赖。
5. 同步空库 schema、测试、Agent 文档及用户使用说明。
6. 执行适用门禁，提交证据后再进入下一阶段。

这些是同一阶段内的提交步骤，不以中间半成品作为可部署完成状态。最终不得保留新旧两套字典运行路径。

## 后续阶段的专项条件

### Phase 2 / Phase 5：前端

- 先本地构建与公共入口，再拆 feature；业务重写与机械拆分不要混在一个大 diff。
- 保留已有房贷/分期等合理模块，逐步取消全局脚本绑定。
- Tailwind 动态 class 显式映射或纳入构建扫描，避免构建后样式丢失。
- 验证 API base、鉴权、CSRF、错误展示、静态资源路径与刷新行为。
- 无网络环境可打开本地核心界面，不出现 Vue/Tailwind/ECharts CDN 请求。
- 图表和弹窗有清晰创建/销毁、事件清理边界；截图/交互验证按阶段明确授权与执行范围。

### Phase 3：导入

- 先列出现有原生 Parser 和 bean-sieve 的格式/银行/摘要/明细能力矩阵。
- 每项旧能力必须有明确归宿：统一 DTO、明示不支持或另行评审的补充明细用例。
- 不把只有明细的输入伪装成完整账单；删除 staging 不得通过更少的测试隐藏能力损失。
- 验证事务中途失败回滚、失败审计、重复来源、重试与并发重复请求。
- IMAP 计数、API 和 UI 按统一导入结果更新，不继续保留 parsed_batches 计数。

### Phase 4：年费

- 定义手动覆盖、实际扣费/返还、参考年费的金额/币种/时间语义。
- 不写 effective 值，不建新子系统，不把参考费率计为实际支出。
- 无历史数据，不编写 dictionary-source 修复或旧值来源猜测逻辑。

### Phase 6：Auth / Runtime

- 移除 admin bearer/token 模式并同步调用方，不建兼容认证分支。
- 保留 Session/CSRF 与 Agent fail-closed allowlist，不把便利性作为放宽权限的理由。
- 后台任务启停、取消、异常隔离与资源释放可验证，不引入额外 worker 服务。

### Phase 7：BillingAccount

- 单独形成领域决策：账户识别依据、同银行多账户、同尾号、多币种账单、额度口径、换卡/销卡与交易归属。
- 无旧数据迁移程序；使用合成 fixture 和现有解析样本验证全新模型。
- CCB、multi-currency、card lifecycle、Statement/Transaction/Repayment 完整性为必需场景。
- 不因删除旧 API 兼容要求而扩大导入时猜测归属的权限；不确定仍需明确处理。

## 验证与发布门禁

开发迭代只跑相关验证；阶段完成时按顺序执行适用门禁。每项结束后再开始下一项，失败即停止。

1. 必要静态/配置检查。
2. 相关 targeted tests。
3. 后端完整测试。
4. 已建立且适用的前端完整测试；尚无 runner 时记录现状与替代检查，不能写成 PASS。
5. fresh DB 初始化、schema 约束和业务不变量；本轮不要求旧库升级或 destructive downgrade。
6. Linux Docker build。
7. 隔离 Docker smoke（临时数据、无真实 provider 调用）；不得连接真实 IMAP/通知/备份凭据。
8. `git diff --check` 与最终 diff 审阅。

执行前检查 runner 和安装版本，确认内部 worker 限制有效。所有测试共享最多两个 worker 的预算，串行敏感用例保持串行；不与其他测试/build/typecheck 并行。保留日志、开始时间、PID/进程组或 unit、退出码和汇总；连接断开只跟踪原任务，不重复启动。

PC/CI 不可用时报告阻塞；不退回 NAS 测试。真实 provider、E2E、压力测试和后台执行按当前任务另行授权。部署与清除临时/共享数据不是门禁的隐式副作用。

## 完成报告模板

- 阶段与精确提交/工作树。
- 实际修改、删除项和非目标是否守住。
- 数据/接口合同变更及调用方是否同步。
- 不变量验证、命令、执行环境、退出码和日志位置。
- 未执行项、阻塞和残余风险。
- 是否仅修改源码，是否涉及镜像、运行实例或数据（不得含糊）。

无历史数据期间可用旧代码/旧镜像在独立空数据环境回退；不自动抹除阶段间新产生的数据。正式发布后另行执行版本化迁移与备份恢复策略。

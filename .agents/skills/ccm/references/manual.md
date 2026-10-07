# Credit Card Manager (CCM) Agent 操作指南

Version: 2026-10-07
标准入口：`python3 scripts/ccm_api.py`
底层脚本：`scripts/ccm_api.py`  
凭据存储：`~/.agents/skills/secrets/ccm.env`

---

## 一、初始化与状态检查

在任务中首次操作 CCM 前，必须先执行 `doctor`：

```bash
python3 scripts/ccm_api.py doctor
```

- 验证局域网网络可达性（`/health` 返回 200）。
- 验证 Agent Token 认证有效性并获取已纳管卡片数。
- 若返回 `status: error`，立即停止并报告，不得盲目重试。

---

## 二、基础操作命令规范

所有对 CCM 的交互必须通过 `scripts/ccm_api.py`，严禁手写 curl、urllib 或临时 HTTP 脚本。

### 1. 查询类操作（GET）

格式：
```bash
python3 scripts/ccm_api.py get --path <API_PATH>
```

常用查询路径：
- **卡片资产**：`get --path /api/v1/cards`
- **账单列表**：`get --path /api/v1/statements`
- **还款提醒与到期金额**：`get --path /api/v1/due`
- **仪表盘聚合指标**：`get --path /api/v1/dashboard`
- **月度消费趋势与统计**：`get --path /api/v1/analytics`
- **交易流水**：`get --path /api/v1/transactions`
- **周期手工账单**：`get --path /api/v1/manual-bills/current`
- **邮件同步状态**：`get --path /api/v1/imap/status`
- **通知推送记录**：`get --path /api/v1/notifications/logs`

### 2. 状态变更类操作（POST / PATCH）

格式：
```bash
python3 scripts/ccm_api.py post --path <API_PATH> [--json '<JSON_PAYLOAD>']
```

常用操作：
- **触发增量邮件同步**：
  ```bash
  python3 scripts/ccm_api.py post --path /api/v1/imap/sync
  ```
- **标记账单全额还清**：
  ```bash
  python3 scripts/ccm_api.py post --path /api/v1/statements/<statement_id>/mark-paid
  ```
- **标记周期手工账单已结清**：
  ```bash
  python3 scripts/ccm_api.py post --path /api/v1/manual-bills/<bill_id>/mark-paid
  ```
- **发送测试通知**：
  ```bash
  python3 scripts/ccm_api.py post --path /api/v1/notifications/test
  ```

---

## 三、核心设计与业务约束（必须遵守）

1. **金额表示（Integer Minor Units）**：
   - 系统内部金额**严格使用整数分**（即 minor units）。
   - 例如：人民币 100.00 元表示为 `10000`；1.50 元表示为 `150`。
   - 严禁向后端提交浮点数金额。

2. **单 Agent Token 最小权限模型（Server-side Allowlist）**：
   - Agent 权限由后端 `AGENT_ROUTE_ALLOWLIST` 严格控制（白名单机制）。
   - Agent **仅拥有日常查询与基础标记权限**。
   - 严禁尝试调用 Owner 专属操作：登录鉴权（`/auth/*`）、Token 轮转（`/agent-token/*`）、WebDAV 备份恢复（`/backups/*`）、删除卡片等。遇此类需求必须向用户说明权限边界。

3. **回读校验（Read-back Verification）**：
   - 执行任何变更操作（标记已还、触发同步、更新卡片属性等）后，必须重新调用对应的 `get` 接口回读最新状态，确认变更生效后方可向用户报告成功。

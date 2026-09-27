# 信用卡管家（Credit Card Manager）

自托管的个人信用卡账单与还款管理工具。
Vue 3 Web 前端 + FastAPI 后端，打包为单个 Python Docker 镜像，数据存储于 本地SQLite。

## 功能

- 卡片与账单管理，支持手动录入账单
- 已支持银行的 EML / PDF 账单导入
- IMAP 邮件自动同步
- 交易分析与还款跟踪
- 还款日提醒
- 内置信用卡字典（银行、卡产品、卡面、年费）

## 构建与运行

仓库根目录放置 `docker-compose.yml`：

```yaml
services:
  credit-card-manager:
    build: .
    image: credit-card-manager:1.0.0
    container_name: credit-card-manager
    restart: unless-stopped
    network_mode: bridge
    ports:
      - "8000:8000"
    volumes:
      - ./data:/data
    environment:
      # 必填：登录验证令牌（首次/管理登录）
      CCM_OWNER_TOKEN: ${CCM_OWNER_TOKEN:?CCM_OWNER_TOKEN must be set}
      # 选填：公网反代域名（纯局域网/内网IP访问留空或不填）
      CCM_ALLOWED_HOST: ""
      # 选填：时区（默认上海时区）
      CCM_TIMEZONE: Asia/Shanghai
```

```powershell
docker compose up --build -d
```

启动时自动完成数据库迁移，浏览器访问 `http://<主机IP>:8000` 即可使用。
请将上述必填令牌换成自己生成的随机值，不要提交到任何仓库。

## 说明

- 纯局域网用户无需配置域名，直接通过内网 IP（如 `http://192.168.x.x:8000`）访问即可。
- 邮箱与备份凭据在 Web 设置页配置，明文存储于 `./data/config/secrets.json`（目录 700 / 文件 600，请勿泄露该文件）。
- WebDAV 备份加密可选：设置备份密码时经 rclone crypt 加密上传；清除密码后直接上传未加密的 SQLite 备份。两种模式存放于不同的远端目录，切换模式不影响已有备份，列表与恢复仅针对当前模式。
- WebDAV 备份仅上传 SQLite 数据库文件，不包含 `./data/config` 下的配置（含 secrets.json）；数据全部保存在 `./data` 目录，整体迁移请备份该目录。
- 如需挂在公网，建议配置反向代理（HTTPS），并在 `CCM_ALLOWED_HOST` 填入对应域名。

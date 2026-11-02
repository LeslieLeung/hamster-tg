# 部署指南

## 1. 获取凭证

需要准备三个凭证：

### BOT_TOKEN

1. 在 Telegram 中搜索 [@BotFather](https://t.me/BotFather) 并发起对话
2. 发送 `/newbot`，按提示设置 Bot 名称和用户名
3. 创建成功后 BotFather 会返回一串 `BOT_TOKEN`，格式类似 `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`

### TELEGRAM_API_ID 和 TELEGRAM_API_HASH

这两个是 Telegram 账号级别的凭证，用于本地 API 服务器认证（与 Bot Token 无关）。

1. 访问 [https://my.telegram.org](https://my.telegram.org)，用手机号登录
2. 点击 **API development tools**
3. 如果没有创建过应用，填写表单创建一个（App title 和 Short name 随意填写）
4. 创建后即可看到 `api_id`（纯数字）和 `api_hash`（字母数字混合串）

## 2. 配置环境变量

复制模板并填入凭证：

```bash
cp .env.example .env
```

编辑 `.env`：

```
BOT_TOKEN=你的Bot Token
TELEGRAM_API_ID=你的API ID
TELEGRAM_API_HASH=你的API Hash
```

## 3. 启动服务

本地直接运行：

```bash
uv run python -m hamster_tg
```

生产环境（拉取预构建镜像）：

```bash
docker compose up -d
```

开发环境（本地构建）：

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

首次启动时 `telegram-bot-api` 需要几秒初始化，`media-bot` 会自动等待并重试连接。`downloads/` 和 `telegram-bot-api/` 目录会由 Docker 自动创建。

## 4. 验证

1. 在 Telegram 中找到你的 Bot，发送 `/start`
2. 发送一张图片，Bot 保存时会短暂显示"正在上传"状态，完成后回复保存确认
3. 批量转发多张图片（相册），Bot 只会回复一条汇总消息，如 `Saved 5 file(s) to default/`
4. 检查宿主机 `downloads/default/` 目录下是否出现文件

## 5. 常用命令

| 命令 | 说明 |
|------|------|
| `/new <name>` | 创建并切换到指定文件夹（支持字母、数字、中文、`-`、`_`） |
| `/newfolder <name>` | 创建并切换到指定文件夹（支持字母、数字、中文、`-`、`_`） |
| `/list` | 查看最近修改的 10 个文件夹 |
| `/status` | 查看当前文件夹和已保存文件数 |
| 直接发送媒体 | 自动保存到当前文件夹，批量转发只回复一次 |

## 6. 停止服务

```bash
docker compose down
```

保存的文件在 `downloads/` 目录中，停止服务不会删除。

## 7. 并发与已有数据升级

在 `.env` 中设置 `DOWNLOAD_CONCURRENCY=2`，默认值也是 `2`，取值必须是正整数。修改后重建或重新创建 `media-bot` 服务使配置生效。

SQLite 数据库为 `downloads/.hamster.sqlite3`，无需额外数据库服务或挂载。首次升级会在后台线程扫描 `downloads/<目录>/` 中的文件，保留原文件和命名，登记内容哈希并恢复文件编号。完成索引的目录可开始处理队列；失败目录等待修复后重新扫描。

使用 `/scan` 扫描全部目录，或 `/scan <目录名>` 扫描指定目录。运行中再次发送 `/scan` 会返回进度。扫描可重复执行，复用未变化文件的哈希；中断或失败保留已提交进度，不删除文件，不执行不完整的缺失清理。正常重启只自动扫描首次索引尚未完成的目录。

`/status` 显示目录文件数、等待/处理中数量、并发数及当前扫描进度。文件数来自数据库索引；手动增删、替换磁盘文件后使用 `/scan` 更新。

当前部署采用单个 `media-bot` 进程。数据库及 WAL 文件需在本机文件系统上。备份时先停止机器人，再备份整个 `downloads/` 目录（包含隐藏数据库及可能存在的 `-wal`、`-shm` 文件），恢复时也一起还原。

首次升级无法恢复旧版内存中的当前目录和失败按钮；新版本保存的目录选择、待办和未过期重试按钮可跨重启恢复。

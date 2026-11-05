> 当前版本 0.1.31-dev.8：已部署，真实 Colab GPU 可用（批次/租约交接、同机复用已恢复的模型）。变更见 [更新说明_main_2026-11-05.md](更新说明_main_2026-11-05.md)。离线/Mock 验收文档仍在 docs/，见 [离线实施记录](docs/OFFLINE_IMPLEMENTATION.md)。

# Colab GPU Studio

用手机就能调 Colab GPU 跑 ComfyUI 的个人 AI 工作台（出图 + H3 视频）。

前端是针对手机触控优化的 React 单页，后端是 FastAPI。平时网页、任务队列、历史作品都存在你自己的小鸡（VPS）上，完全不占 GPU；只有你点下“生成”的那一刻，后端才会去向 Google Colab 申请 GPU，跑完把成品存回来，**立刻销毁 runtime，绝不让你的计算单元（CU）空转白扣费**。

## 为什么做这个？

1. **省 CU 是第一生产力**：ComfyUI 环境和几十 GB 的大模型权重全部缓存在你的 Google Drive 里。下模型、装环境这种脏活累活交给便宜的 CPU runtime 去干；贵的 GPU（T4 / L4）开机只做三件事：**还原环境 → 采样出片 → 马上关机删实例**。
2. **手机上也能舒服捏图/跑视频**：不用在手机浏览器里死磕密密麻麻的 ComfyUI 节点连线，也不用一直盯着 Colab 标签页防断连。前端会根据工作流自动渲染表单，关掉浏览器后台照样排队跑。
3. **自带 4 套打磨好的工作流模板**：开箱内置 Krea+MOSS 双采出图、MOSS 文生图、MiniMax H3 首尾帧图生视频、MiniMax H3 多参考图生视频（支持最多 6 张参考图、8步 Turbo v1.1、1.2x 潜空间精修、二次元无缝 VAE 及多款 SFW 动作/运镜/画风锁定 LoRA）。

> **安全提示**：新安装默认只开启 `demo-mock` 离线演示（不连 Google、不扣点数）。内置的 4 套真实工作流会以“草稿”形式躺在工作流列表里，需要你绑好自己的 Google OAuth、确认模型许可并跑通一次测试后才会正式启用，防止刚装好误触直接烧掉 CU。

## 内置工作流一览

| 工作流 | 版本 | 推荐显卡 | 说明 |
|---|---|---|---|
| `krea2-moss-dual-t2i` | 1.0.0 / 1.1.0 | T4 HighRAM | 1.0.0 原模版；1.1.0 全链路串联 7 个 MJ 与胶片精修 LoRA，大幅提升出图画质 |
| `moss-image-sft-t2i` | 1.0.0 | T4 | OpenMOSS 单张文生图 |
| `minimax-h3-i2v` | 1.1.0 | L4 | 首帧（可选尾帧）生成有声短视频，带 8 步加速 LoRA |
| `minimax-h3-ref2v` | 1.4.0 | L4 | 最多 6 张参考图生成有声视频，可选 4步/8步/精修/20步 与二次元无缝 VAE |

详细说明与启用步骤见 [内置工作流说明](docs/BUILTIN_WORKFLOWS.md)。

## 快速部署

### 方式一：Docker 一键启动（推荐 VPS 部署）

需要先装好 Docker、Docker Compose 和 Python（用于生成初始随机密钥）：

```bash
# 1. 生成你的私密 .env 配置文件（把域名换成你自己的，本地玩就不改）
uv run python scripts/init_env.py --site-url https://你的域名

# 2. 启动服务（包含 api / worker / watchdog 三个容器）
docker compose up -d --build
```

服务默认只监听本机 `127.0.0.1:8788`，公网访问建议用 Caddy 或 Nginx 反代并套上 HTTPS。数据全部存在 `studio-data` 卷里，**千万别手滑敲 `docker compose down -v` 把数据卷删了**。

### 方式二：本地直接跑

需要 Python 3.11+、`uv`、Node.js 和 `npm`：

```bash
uv sync --extra test
uv run python scripts/init_env.py --site-url http://127.0.0.1:8788
npm --prefix web ci
npm --prefix web run build

# 终端 1：启动 Web API
uv run --env-file .env uvicorn app.main:app --app-dir api --host 127.0.0.1 --port 8788 --no-access-log

# 终端 2：启动任务队列 Worker
uv run --env-file .env python -m app.worker worker

# 终端 3：启动超时清理看门狗 Watchdog
uv run --env-file .env python -m app.worker watchdog
```

启动后打开 `http://127.0.0.1:8788`，登录密码在刚生成的 `.env` 文件里的 `STUDIO_PASSWORD` 字段。

## 怎么接自己的 Google Colab 跑真实模型？

为了防止密钥泄露和乱扣费，本项目默认关闭真实 GPU（`GPU_REAL_ENABLED=0`）。接入你自己的账号只需要按顺序走一遍：

1. **绑定 Google OAuth**：按 [Google OAuth 与 Drive 配置](docs/OAUTH_SETUP.md) 在网页设置里导入你的 Google Cloud Web 客户端 JSON，分别点击连接 Colab 和 Drive。
2. **启用内置工作流或导入新工作流**：参考 [内置工作流说明](docs/BUILTIN_WORKFLOWS.md) 和 [模型与工作流审查流程](docs/WORKFLOW_REVIEW.md)，开启 `GPU_REAL_ENABLED=1`、填好预算限额，审核并启用工作流。

## 开发与测试

```bash
uv run pytest -q
uv run ruff check api tests
npm --prefix web run test:contract
npm --prefix web run build
```

## 注意事项与免责声明

- **管好你的密钥**：千万不要把 `.env`、数据库、Google OAuth 客户端 JSON 传到公开仓库或发给别人。`.env` 里的 `ENCRYPTION_KEY` 是用来解密数据库里 OAuth 令牌的，建议离线备份一份，丢了就得重新授权。
- **计费以 Google 为准**：网页上的 CU 消耗只是根据历史任务算出来的估算值，实际扣多少计算单元请以 Google Colab 后台为准。
- **个人自用定位**：本项目是为个人自用设计的，没做多租户隔离，别直接拿去当公开的多人 SaaS 站开。
- **许可证**：本项目代码采用 **GNU AGPL-3.0** 许可证开源（修改后通过网络对外提供服务同样须开源修改后的完整源码）；工作流里用到的 ComfyUI、第三方节点、模型权重与 LoRA 各自遵循它们原本的许可证（仓库内不含任何模型权重文件）。

## 支持社区

- [LINUX DO](https://linux.do)

欢迎大家提 Issue、发 PR 一起完善功能或分享更好用的工作流！如果觉得有用，欢迎点个 Star 支持一下~

### 可重复离线验收（不启动真实 GPU）

依赖安装完成后运行 `uv run python scripts/verify_offline.py`。该命令隔离配置，执行后端测试、Ruff、前端契约及构建，并自行启动/关闭两套临时 Mock 服务，运行批次、失败重试及长列表浏览器测试。浏览器来源必须为本机 `127.0.0.1`；可通过 `OFFLINE_CHROMIUM` 指定 Chromium 可执行文件。测试日志与网络审计保存在 `docs/offline-evidence/`，仅所有检查通过后写入 `verification-run.json`。

`.github/workflows/offline.yml` 提供同一命令的 CI 配置；当前仅本地执行验证，尚未在 GitHub Actions 上运行。依赖安装会访问软件包仓库，但验收阶段禁止真实 Colab、OAuth、Drive 业务及 GPU 调用。此验收不证明真实云存储或模型可用。

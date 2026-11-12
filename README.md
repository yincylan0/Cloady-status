# Kerit 容器状态面板

无第三方依赖的只读 Python 状态页，提供 `/`、`/api/status` 与 `/healthz`。页面为中文深色主题，展示当前进程与容器可见的系统信息、常用 CLI 和运行时版本；页面打开后立即向同源服务读取状态，此后每 60 秒刷新一次。轮询失败会保留最近一次成功显示的数据，并在后续周期重试；浏览器后台标签页可能对定时器节流，因此间隔并非实时性保证。容量按 1024 进位，运行时间以易读格式显示。源码位于 [yincylan0/-kerit-status](https://github.com/yincylan0/-kerit-status)。本项目改编自 [mathowvan/orkestr-status](https://github.com/mathowvan/orkestr-status)。

## 部署到 Kerit

应用监听 `PORT` 环境变量，未设置时依次读取 `SERVER_PORT`，都没有时默认使用 `8000`。健康检查端点 `/healthz` 可独立访问。

部署方式取决于在 Kerit 上开通的服务器类型：VPS（带独立 IPv4）可直接运行；面板（Pterodactyl）服务器需确认其 egg 是否分配公开端口、启动命令能否自定义。开通服务器后，按所选方式把本仓库文件传到服务器并用下面的命令启动。

## 通过 Git 仓库与 Dockerfile 部署

Dockerfile 位于仓库根目录，支持 Docker 构建的平台会自动使用它构建。应用监听 `PORT` 环境变量；未设置时默认使用 `8000`。健康检查端点 `/healthz` 可独立访问；这里不依赖任何特定的平台健康检查路径设置。

构建使用官方 `python:3.13-alpine` 镜像并通过 `apk upgrade --no-cache` 升级 OS 包。独立测试阶段运行全部 Python 单元测试，只有测试成功构建才会继续；最终镜像从更新后的 Alpine 基础阶段派生，只复制应用与轮询脚本，不包含测试文件。Alpine 使用 musl libc 而非 glibc；本应用目前不使用 pip 依赖或原生扩展，但其他依赖或运行时假设可能需要额外验证。

容器以非 root 用户运行；默认不采集网络地址。服务不需要密钥，也不接受用户指定的命令、URL 或文件路径。

## 本地运行与测试

```sh
PORT=8000 python3 -I -B -u kerit_status.py
python3 -I -B -m unittest -v
node --test poll.test.js
```

Node.js 仅用于运行前端行为测试，不是应用运行依赖，也不会加入生产镜像。轮询请求在 15 秒后超时，保留旧数据并等待下一周期重试。

## 指标范围与安全提示

这些是容器/进程可见的指标，不代表 Kerit 账户、服务商层面的可用性、运行时间或 uptime，也不是平台监控或告警保证。隔离、cgroup 与挂载视图会影响 CPU、内存、磁盘及运行时间读数；部分字段可能不可用。网络详情默认关闭，只有显式设置 `STATUS_NETWORK_DETAILS=1` 才进行本地地址解析和固定 HTTPS 公网 IP 查询。

状态 API 未提供身份认证；若公开服务地址，任何人都可能读取系统指标和已识别的软件版本。请勿与敏感业务或凭据共置。

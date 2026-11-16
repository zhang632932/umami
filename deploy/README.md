# Umami GitHub 自动部署

服务器每天北京时间 04:00 检查公开仓库 `zhang632932/umami` 的 `master`（调度精度 1 分钟）。停机或停用期间错过的检查，会在定时器恢复后补执行一次。新提交先在 `/www/wwwroot/umami/source` 构建 `umami-local:<完整 commit>`；构建成功后才暂停生产应用、备份数据库，在独立候选容器中完成迁移和验证，最后切换生产应用。候选容器仅绑定随机的本机回环端口，公网无法向它发送统计事件。Docker 镜像内自带 Node，无需升级宿主机 Node。

仅允许版本号为稳定的 `3.x.y`；v4、预发布版本、Git 分支分叉和本地源码改动都会阻止部署。部署只 recreate `umami` 服务，不更新 PostgreSQL 12.22 或删除数据库卷。镜像启动会自动应用 Prisma migrations。

## 安装

将 Python 文件安装为 `/usr/local/sbin/umami-deploy.py`（root:root、755），service/timer 安装到 `/etc/systemd/system/`。生产 compose 必须保留当前数据库连接及 APP_SECRET，宿主机映射 `3100:3000`。首次执行会部署当前 master，即使还没有新的推送。

```sh
python3 -m py_compile /usr/local/sbin/umami-deploy.py
systemctl daemon-reload
systemctl start umami-deploy.service
systemctl status umami-deploy.service
# 首次部署成功、确认应用正常后启用定时任务
systemctl enable --now umami-deploy.timer
```

Python 使用标准库，依赖宿主机 Git、Docker Compose v2 和容器内 PostgreSQL 工具。Git origin 必须是上述公开 HTTPS 地址，服务器不需要 GitHub 私钥。推送后在下一次每日检查时开始构建；构建时间另计。需要立即检查时可手动执行 `systemctl start umami-deploy.service`。

本机验证部署状态机：`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s deploy -p 'test_*.py' -v`。测试模拟数据库/容器，并使用真实子进程检查日志保存，不连接生产服务器。

## 状态和操作

```sh
journalctl -u umami-deploy.service -n 80 --no-pager
systemctl list-timers umami-deploy.timer
cat /www/wwwroot/umami/deploy-state/success.json
cat /www/wwwroot/umami/deploy-state/failure.json
# 修复构建/运行问题后重试同一提交
/usr/local/sbin/umami-deploy.py --retry
# 恢复中断的切换，不检查新代码
/usr/local/sbin/umami-deploy.py --recover-only
# 暂停未来自动检查（正在执行的部署会继续）
systemctl stop umami-deploy.timer
```

构建详细输出写入 `deploy-state/build-<commit>.log`，其他失败命令输出写入 `deploy-state/last-command.log`，权限 600；日志可能含工具输出的配置细节，不要公开或提交进 Git。systemd journal 仅记录操作结果及诊断文件路径。

构建和部署由同一文件锁互斥。失败提交会被记录，后续每日检查不会反复构建/迁移；推送新提交或执行 `--retry` 才重试。事务 marker 保存在 `deploy-state/transaction.json`；异常中断后下一次运行优先恢复，不会直接开始另一次部署。

## 备份、验证和回滚

每次切换前创建 `/www/backup/umami/<UTC 时间>/`，权限 700，包含数据库 custom dump、生产 compose、已有镜像 override、`.env`（如存在）以及旧镜像 ID/本地 rollback tag。文件权限 600。成功记录和 marker 使用原子写入及 fsync；备份和旧镜像不会自动清理。

候选验证包括容器存活、`/api/heartbeat`、`/login`、`/script.js`、Prisma 无未完成迁移及用户/网站数量一致。heartbeat 本身不检查数据库。候选环境从生产 Compose 解析，临时环境文件权限 600，候选退出后删除；诊断输出保存在备份目录的 `candidate.log`，权限 600。

发布到公网前失败：删除候选容器，用 `pg_restore --create --clean --if-exists --exit-on-error -d postgres` 恢复完整数据库，核对网站/用户数量，再启动旧镜像并验证。数据库恢复成功后、旧应用启动前立即记录 `database_restored`，防止异常中断后重复恢复并覆盖重新采集的事件。

切换到公网前会先持久写入 `publishing`。从这一步开始，新应用可能已经采集事件，恢复过程只会继续启动和检查新镜像，**不会恢复旧数据库**。重复失败会保留 marker、日志和备份，供人工修复；后续运行继续尝试恢复新应用。公网阶段检查数据库连接和迁移状态，允许管理员合法增删网站或账户。完成后原子记录成功 revision。

生产基础 compose 保留在原位置。应用镜像 override 为 `/www/wwwroot/umami/docker-compose.override.yml`，在生产目录执行普通 `docker compose` 时会自动加载；显式指定文件时同时传两个配置：

```sh
docker compose -p umami -f /www/wwwroot/umami/docker-compose.yml \
  -f /www/wwwroot/umami/docker-compose.override.yml ps
```

备份包含数据库和可能含凭据的环境配置，请保持现有权限，不要提交进 Git。升级 PostgreSQL major 需要独立 dump/restore，不应直接修改数据库 image tag 并沿用旧 volume。

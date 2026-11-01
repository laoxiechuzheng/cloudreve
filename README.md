需求台账：[REQUIREMENTS.md](REQUIREMENTS.md)（维护者本地私有文件，不提交到公开仓库）。

# Cloudreve Custom 3.8.3

基于 Cloudreve **3.8.3** 的定制发行版：保留原有数据格式、API、分享链接和权限逻辑，统一文件管理、登录、分享和管理后台的界面，通过 GitHub Actions 编译完整程序，并提供 systemd 部署和数据迁移工具。

## 源码与版本

| 部分 | 基线 / 位置 |
| --- | --- |
| 后端 | 官方标签 `3.8.3`，提交 `88409cc1f0a0a55b7ea8f0aee15e82876260b2d8` |
| 前端 | `assets` 子模块；官方基线 `5d4d01a797a1ba2d6866799684bf05de20006e31` |
| 定制分支 | 两个仓库均为 `custom/3.8.3` |
| 数据库版本 | `3.8.1`，保持原版要求，不执行跨版本迁移 |
| 部署工具 | [deploy/README.md](deploy/README.md) |
| 原版说明与许可 | [README.upstream.md](README.upstream.md)、[LICENSE](LICENSE) |

前端 fork：[laoxiechuzheng/cloudreve-frontend](https://github.com/laoxiechuzheng/cloudreve-frontend)。后端固定引用前端提交，因此每份发行包都有明确的两端源码版本。

## 界面

- 统一明暗主题、导航、登录与账户相关页面、后台布局及控件。
- 文件管理器提供可见的逐项下载/分享操作、当前目录筛选、明确的视图切换和分页。
- 下载分享页位于前端 `public/share-ui/`，沿用 `/s/:id`、`?path=` 和提取码；`?classic=1` 返回应用内分享视图。
- 预览、上传、下载、验证码、两步验证和 WebAuthn 沿用现有接口。
- Service Worker 不拦截请求；激活时清理原版 Workbox 缓存，避免旧页面缓存遮住更新。

## 构建与发布

```sh
git clone --branch custom/3.8.3 --recurse-submodules https://github.com/laoxiechuzheng/cloudreve.git
cd cloudreve
```

推送定制分支触发 **Custom build**：恢复锁定的前端依赖，运行前端、Go 和部署工具测试，编译嵌入前端的 Linux amd64/arm64 二进制。推送 `v3.8.3-custom.*` 标签还会创建 GitHub Release。工作流只使用仓库内置 `GITHUB_TOKEN`，无需另配发布密钥。

产物包括 `cloudreve_3.8.3-custom.<版本>_linux_<架构>.tar.gz` 和 `SHA256SUMS`。包内的 `build-info.json` 记录后端/前端提交、Go 版本、架构和二进制摘要。后端对外版本仍为 3.8.3，以保持客户端兼容。

编译使用固定的 Go 1.20.14、Node 20.19.5、Yarn 1.22.22 和上游锁文件。这些只是构建环境；生产机器不需要 Go、Node、Yarn 或 Docker。既有上游依赖没有被批量升级。

## 运行与迁移

运行依赖为 Linux、systemd、Python 3.9+。先在隔离环境验证下载包与数据快照，再按 [部署指南](deploy/README.md) 安装。

```sh
python3 deploy/cloudreve-manager.py --root /opt/cloudreve status
python3 deploy/cloudreve-manager.py --root /opt/cloudreve install --archive /path/to/release.tar.gz --sha256 <SHA256>
python3 deploy/cloudreve-manager.py --root /opt/cloudreve install-service
```

管理工具要求已有且兼容的配置/数据库，拒绝对正在被非托管进程使用的数据直接切换。首次从 tmux 转交时，需要先核对并优雅停止原进程。systemd 开机自启、异常重启，日志通过 `journalctl -u cloudreve-custom` 查看。

数据目录中的 `conf.ini`、`cloudreve.db`、`uploads/`、`avatar/`、`temp/` 保留。程序放在 `releases/<版本>/`，`current` 指向生效版本。Cloudreve 按可执行文件位置读取相对路径，因此各版本目录的数据链接始终指向同一份数据。旧的根目录二进制和 `statics/` 可以保留为首次迁移回退点；它们不会遮住新版本的内嵌界面。

导出/恢复工具会校验快照摘要和路径、检查数据库完整性，停止托管服务以取得一致快照，并在完成或失败时恢复原运行状态；只允许恢复到空目录，不自动删除数据备份。保持 `HashIDSalt` 和会话密钥才能保留分享 ID 和会话语义。使用相对路径的本地存储可迁移，外部数据库、远程存储和非标准绝对路径会明确拒绝，避免遗漏数据。

反向代理配置及 HTTPS 证书由运维管理，参考部署目录中的 nginx 模板。迁移到新 IP 后，需更新域名解析或入口代理；若旧链接直接使用旧 IP，主机地址的延续需保留该 IP 或旧入口，应用数据迁移本身只能保留分享路径和 ID。

## 维护约定

- 所有生产配置、数据库、用户文件、私有需求台账和备份都留在部署目录，不进入 GitHub。
- 程序回滚仅切换已验证版本，不覆盖数据库。恢复旧数据必须作为单独明确的操作。
- 修改前端后先提交并推送前端仓库，再更新本仓库子模块指针；CI 使用该固定提交构建。
- 不使用上游 Docker 发布流程或另一个大版本的发布包代替本工作流产物。

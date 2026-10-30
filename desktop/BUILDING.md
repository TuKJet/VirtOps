# 构建 VirtOps Desktop

VirtOps Desktop 是一个 x64 桌面客户端。它调用确定性的 VirtOps 控制逻辑；不需要 AI 服务或模型后端。

首次本机使用从用户选择 Windows ISO 开始，基础镜像由客户端在本机制作和复用。GitHub 只分发客户端源码及免安装程序，不存放 Windows ISO、基础镜像、VM 数据或用户凭据。客户端与操作系统安装介质是两个独立输入。

## 支持范围

- Windows：CI 只产出 Windows x64 的单文件 `portable.exe`。目标用户系统为 Windows 10/11 x64。
- Linux：CI 在 Ubuntu 22.04 x64 只构建 `.AppImage`，应先在目标发行版验证。
- 当前所有自动构建产物都是**未签名**的测试产物。Windows 可能显示 SmartScreen/发布者未知提示；不要把它们当作正式发布包。
- Windows 本机 VM 需要专业版/企业版、管理员权限和已启用的 Hyper-V；家庭版可作为远端客户端。当前开发机为家庭版，本机真实安装/启动/隔离未验收。Linux 本机目前管理已有 KVM 实例，不包含 ISO 制作向导。

下载入口是 [GitHub Releases](https://github.com/TuKJet/VirtOps/releases)。Windows 下载 `windows-x64-portable.exe` 双击运行；Linux 下载 `.AppImage`，首次在文件属性中允许作为程序执行，之后双击运行，也可用 `chmod +x VirtOps-Desktop-*.AppImage` 设置执行权限。项目不再生成或发布 setup.exe、MSI、DEB。

便携版直接发布 Tauri release 构建的 `desktop/src-tauri/target/release/virtops-desktop.exe`，前端和 PowerShell 管理脚本已编入程序，无需旁边放置脚本目录，也不要求用户安装 Node.js、Python 或 Rust。系统必须已有 WebView2 Runtime；便携版不会运行依赖安装器。它仍使用应用数据目录保存配置、模板和实例；删除或移动 EXE 不会删除这些数据。

GitHub Actions 每次推送 `main` 或 `codex/desktop-clients`、相应 pull request，以及手动触发时都会重新构建。推送和在这两个分支手动运行时，两个平台构建均成功才发布独立的预览 Release，标签包含版本和提交 SHA。先建立草稿，上传便携 EXE、AppImage 及 `SHA256SUMS.txt`，核对 GitHub 记录的文件大小和 SHA-256 后公开。缺少任一客户端，或混入 setup.exe/MSI/DEB 等额外文件，都会阻止发布。重复运行同一提交会核对既有文件，不覆盖已公开的 Release。PR 不发布，只有发布 job 具有 `contents: write`。

工作流 artifact 仍保留 14 天用于排查；Release 文件不受该期限影响。流程不部署 VM。预览版不会被标为正式 Latest Release，也不代表客户端内自动升级已实现。

## 本地前置条件

- Node.js 24.22.1（CI 锁定该版本）
- Rust stable，且不低于本项目开发时的 Rust 1.96
- Windows：Edge WebView2；开发构建另需 C++ Build Tools，普通用户无需安装编译工具。
- Ubuntu 22.04：`libwebkit2gtk-4.1-dev libappindicator3-dev librsvg2-dev patchelf xdg-utils`

安装依赖并验证：

```powershell
Set-Location desktop
npm ci
npm run build
Set-Location src-tauri
cargo test --locked
cargo check --locked
```

构建 Windows 单文件 EXE：

```powershell
Set-Location desktop
npm run tauri -- build --no-bundle
```

构建 Linux AppImage：

```bash
cd desktop
npm run tauri -- build --bundles appimage
```

`npm run tauri` 是项目锁定版本的 Tauri CLI；因此必须提交 `package-lock.json`，而 Rust 命令要求提交 `Cargo.lock`。

## 系统运行组件

Windows 便携 EXE 使用系统已有的 WebView2 Runtime，不包含浏览器运行库，也不负责下载或安装运行库。离线部署需预先在宿主机准备 WebView2。管理本机 VM 还需已启用 Hyper-V；使用远端模式需系统 OpenSSH。

Linux AppImage 内含客户端及打包依赖，仍要求兼容的 Linux 桌面系统。执行权限、FUSE 和发行版兼容性需在实际目标环境验证；本机 VM 由已有 KVM/VirtOps 环境提供。

官方参考：

- [Tauri Windows Installer / WebView2 modes](https://v2.tauri.app/distribute/windows-installer/)
- [Tauri GitHub Actions pipeline](https://v2.tauri.app/distribute/pipelines/github/)
- [GitHub Actions workflow artifacts](https://docs.github.com/actions/using-workflows/storing-workflow-data-as-artifacts)

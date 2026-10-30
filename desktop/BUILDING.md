# 构建 VirtOps Desktop

VirtOps Desktop 是一个 x64 桌面客户端。它调用确定性的 VirtOps 控制逻辑；不需要 AI 服务或模型后端。

首次本机使用从用户选择 Windows ISO 开始，基础镜像由客户端在本机制作和复用。GitHub 只分发客户端源码及安装包，不存放 Windows ISO、基础镜像、VM 数据或用户凭据。软件安装包与操作系统安装介质是两个独立输入。

## 支持范围

- Windows：CI 产出 Windows x64 的 NSIS `setup.exe` 和 MSI 安装包。目标用户系统为 Windows 10/11 x64。
- Linux：CI 在 Ubuntu 22.04 x64 构建 `.deb` 与 `.AppImage`。`.deb` 面向 Debian/Ubuntu 系；AppImage 应先在目标发行版验证。
- 当前所有自动构建产物都是**未签名**的测试产物。Windows 可能显示 SmartScreen/发布者未知提示；不要把它们当作正式发布包。
- Windows 本机 VM 需要专业版/企业版、管理员权限和已启用的 Hyper-V；家庭版可作为远端客户端。当前开发机为家庭版，本机真实安装/启动/隔离未验收。Linux 本机目前管理已有 KVM 实例，不包含 ISO 制作向导。

安装包入口是 [GitHub Releases](https://github.com/TuKJet/VirtOps/releases)。Windows 通常选择 `windows-x64-setup.exe`；其他格式按文件名选择。

GitHub Actions 每次推送 `main` 或 `codex/desktop-clients`、相应 pull request，以及手动触发时都会重新构建。推送和在这两个分支手动运行时，两个平台构建均成功才发布独立的预览 Release，标签包含版本和提交 SHA。先建立草稿，上传四个安装包及 `SHA256SUMS.txt`，核对 GitHub 记录的文件大小和 SHA-256 后公开。重复运行同一提交会核对既有文件，不覆盖已公开的 Release。PR 不发布，只有发布 job 具有 `contents: write`。

工作流 artifact 仍保留 14 天用于排查；Release 安装包不受该期限影响。流程不部署 VM。预览版不会被标为正式 Latest Release，也不代表客户端内自动升级已实现。

## 本地前置条件

- Node.js 24.22.1（CI 锁定该版本）
- Rust stable，且不低于本项目开发时的 Rust 1.96
- Windows：用于开发/运行的 Edge WebView2 与 C++ Build Tools；MSI 构建还需要系统启用 VBSCRIPT 可选功能。
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

打 Windows 包：

```powershell
Set-Location desktop
npm run tauri -- build --bundles nsis,msi
```

打 Ubuntu 包：

```bash
cd desktop
npm run tauri -- build --bundles deb,appimage
```

`npm run tauri` 是项目锁定版本的 Tauri CLI；因此必须提交 `package-lock.json`，而 Rust 命令要求提交 `Cargo.lock`。

## WebView2 的离线取舍

当前 Windows 配置未固定 WebView2 的安装模式。发布前需按交付环境明确选择：

- `downloadBootstrapper` 是较小的默认模式，但首次安装需要联网。
- 隔离/离线现场使用 `offlineInstaller`；安装包约增加 127 MB。
- `fixedRuntime` 也可完全离线，但约增加 180 MB，并将 WebView2 漏洞修复责任转给应用发布方。

不要使用 `skip`，除非部署方已明确保证每台目标机均存在可用 WebView2 Runtime。

官方参考：

- [Tauri Windows Installer / WebView2 modes](https://v2.tauri.app/distribute/windows-installer/)
- [Tauri GitHub Actions pipeline](https://v2.tauri.app/distribute/pipelines/github/)
- [GitHub Actions workflow artifacts](https://docs.github.com/actions/using-workflows/storing-workflow-data-as-artifacts)

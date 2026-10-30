# VirtOps

## 桌面客户端：Windows / Linux

`desktop/` 是 Tauri 2 + Rust + WebView 桌面客户端，使用固定代码执行操作，不需要 AI 服务。默认面向只有一台工作站的用户，远端服务器是可选连接。

Windows 本机初始化从**用户自己的 Windows ISO** 开始：读取专业版/企业版索引 → DISM 安装到新建 VHDX → 无网络的专用 Hyper-V 构建 VM → 清理临时账户并 Sysprep → 校验、封存本地只读模板。之后从模板创建独立磁盘、用户、密码、TPM 和隔离网络的 VM，删除实例后可再次创建。不会上传 ISO、模板、虚拟磁盘或用户数据。模板默认保存在本机应用数据目录的 `hyperv/images`，实例在 `hyperv/instances`；已有 VHDX 导入仅为引擎高级入口。

| 使用方式 | 当前桌面能力 |
|---|---|
| Windows 10/11 专业版、企业版本机 | Hyper-V 环境检查、ISO 构建模板、创建/确认删除实例、管理已登记实例 |
| Linux 本机 | 导入本机已有 VirtOps/KVM 实例 profile 并管理；首次 ISO 安装仍使用已有 Skill 流程 |
| Windows / Linux 连接服务器 | SSH 管理 Linux/KVM 上已有 VirtOps 实例，受控 guest 操作使用 QEMU Guest Agent |

实例管理包括状态、启停、资源、系统 RDP 客户端、多个 TCP 入站端口发布/撤销、目录查询、阻断日志。端口发布同时处理 guest 防火墙、宿主入口和客户端回环转发，保留出站隔离；TCP 连通不等于 HTTP/MCP 认证成功。应用及 MCP 的安装、Token 和功能仍属于外部应用。账户变更、文件上传下载、Linux 模板制作等完整向导尚未覆盖。

本机 Hyper-V 要求已启用系统功能、管理员运行及足够空闲资源，客户端不自动启用功能或重启电脑。远端模式需导入实例 JSON，并已有 OpenSSH、已知宿主密钥和 SSH 认证。配置不存明文密码；Windows 本机实例凭据用 DPAPI 加密。任务按 VM 串行执行，本机 Hyper-V 另有跨进程互斥；退出客户端会关闭其管理的转发。

这是开发预览分支：合约测试、模拟界面和编译验证不能替代专业版/企业版机器上的真实 DISM、OOBE、PowerShell Direct、Sysprep、两实例隔离和 RDP 验收。当前开发电脑是家庭版，未运行本机建 VM 验收。已有远端实例只完成了读取状态、服务和目录的验证，未为客户端开发修改生产 VM。

安装包从 [Releases](https://github.com/TuKJet/VirtOps/releases) 下载。Windows 优先选择 `windows-x64-setup.exe`，也提供 MSI；Linux 提供 `.deb` 和 `.AppImage`。每次推送 `main` 或 `codex/desktop-clients`，GitHub Actions 都会测试并编译；两个平台全部成功后，发布绑定该提交的独立预览 Release，附带四个安装包和 `SHA256SUMS.txt`。上传后的文件大小和 SHA-256 校验通过才公开发布。PR 仅构建，不发布。Actions Artifacts 另外保留 14 天；Release 安装包不会随 Artifact 到期而删除。构建不连接服务器，不包含运行配置或凭据，也不部署 VM。

详见 [构建与下载说明](desktop/BUILDING.md) 和 [界面规范](DESIGN.md)。

## Skill

给 AI agent 使用的 Windows/KVM Skill：**基础系统只制作一次，多台 VM 复用系统镜像，每台使用独立账户、密码、磁盘和网络；需要干净环境时删除实例，再从基础镜像开出新的。**

资源调整、账户修改、镜像制作、实例创建和删除都由工程自带命令完成，不要求 agent 在用户工作目录临时编写运维脚本。

## 生命周期

1. 用 Windows 官方 ISO 安装一台专用、可丢弃的构建 VM，准备驱动、QGA 和批准的软件。
2. 自带 `template.py` 检查构建 VM，移除其临时账户和实例秘密，执行 Sysprep generalize/OOBE/关机，核验离线 Windows 状态后发布只读镜像。
3. 自带 `instance.py create` 从同一镜像物化独立 qcow2，为新实例生成独立 UUID、MAC、NVRAM、TPM、网络、凭据和 HTTPS 证书。
4. 自带 `destroy.py` 完整删除一个实例；`instance.py rebuild` 校验替代配置后删除旧实例，并以新身份重新创建。**系统、安装软件和数据都会清除。**共享基础镜像和其他实例保留。

新实例不建立 C 恢复 WIM，不克隆运行中的个人系统，不复制 TPM/NVRAM/密码，不默认使用相互依赖的 qcow2 overlay。旧版 C-only reset 仅保留兼容已有部署的实现。

完整操作、续作与验证见 [基础镜像与实例](skill/windows-vm-sandbox/references/images-instances.md)。已有 VM 的保留系统维护见 [资源与账户](skill/windows-vm-sandbox/references/configure.md)。

## 安装 Skill

```sh
python install.py --check
python install.py --install
python install.py --verify-installed
```

默认安装到 `$CODEX_HOME/skills/windows-vm-sandbox`，未设置时使用 `~/.codex/skills`。其他 agent 用 `--skills-dir` 指定技能父目录。升级用 `--upgrade`，不覆盖用户手改文件。安装只复制 Skill，不连接服务器或修改 VM。Profile、凭据、镜像和操作记录放在工程之外。

## 让 agent 做什么

- “从基础镜像给甲乙各开一台 Windows，资源分别是……，使用不同用户名密码，端口各自独立。”
- “删掉 alice-dev 的全部数据，按原规格从基础镜像重开。”
- “复用这个 VM 的系统，把内存改为 64G，管理员改为 alice。”
- “查看所有实例和总资源预算，上传脚本到指定实例执行。”
- “给这台 VM 发布一个 MCP/HTTP 服务，自动分配独立端口，保留出站隔离。”

额外 TCP 入站服务由每台实例的 `network.published_services` 声明。Windows 客户端使用 `service.py publish --profile PROFILE.json --guest-port PORT --apply`，自动配置 Windows 入站规则、宿主发布和本机 SSH 转发。不同 VM 可以使用相同的内部应用端口，宿主端口自动避让；维护、守卫与回滚使用同一配置，详见[受控入站服务](skill/windows-vm-sandbox/references/published-services.md)。

未指定的关键资源、GPU、账户和入口由 agent 集中确认；名称/UUID 等实现细节可以生成。用户明确要求的范围内自动执行，只有缺输入、身份不符、容量不足或未知数据引用时停止。

支持目标为 Ubuntu 24.04/x86_64/systemd/cgroup v2/monolithic libvirtd；软件图形或显式专供 GPU。不会自动改变其他任务、共享 libvirt 服务、全局防火墙策略或宿主 GPU 驱动。

## 验证

```sh
python -X utf8 -m unittest discover -s tests -v
python install.py --check
```

Windows 另运行 `tests/guest_tools_test.ps1` 和 `tests/guest_behavior_test.ps1`。模拟测试证明命令、身份边界、渲染及故障行为；真实的 Sysprep 首启、两台 VM 独立认证、桌面与网络隔离仍须按 [验收边界](VALIDATION.md) 执行，不能以模板测试代替。

仓库不包含系统镜像、虚拟磁盘、用户数据或真实凭据。

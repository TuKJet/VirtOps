# VirtOps

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

额外 TCP 入站服务由每台实例的 `network.published_services` 声明。已有实例通过自带 `publish_services.py` 在线发布或撤销，默认使用宿主回环和 SSH 隧道，并从 18000–18999 自动选择未占用的端口。不同 VM 可以使用相同的内部应用端口；发布、维护、守卫与回滚使用同一配置，详见[受控入站服务](skill/windows-vm-sandbox/references/published-services.md)。

未指定的关键资源、GPU、账户和入口由 agent 集中确认；名称/UUID 等实现细节可以生成。用户明确要求的范围内自动执行，只有缺输入、身份不符、容量不足或未知数据引用时停止。

支持目标为 Ubuntu 24.04/x86_64/systemd/cgroup v2/monolithic libvirtd；软件图形或显式专供 GPU。不会自动改变其他任务、共享 libvirt 服务、全局防火墙策略或宿主 GPU 驱动。

## 验证

```sh
python -X utf8 -m unittest discover -s tests -v
python install.py --check
```

Windows 另运行 `tests/guest_tools_test.ps1` 和 `tests/guest_behavior_test.ps1`。模拟测试证明命令、身份边界、渲染及故障行为；真实的 Sysprep 首启、两台 VM 独立认证、桌面与网络隔离仍须按 [验收边界](VALIDATION.md) 执行，不能以模板测试代替。

仓库不包含系统镜像、虚拟磁盘、用户数据或真实凭据。

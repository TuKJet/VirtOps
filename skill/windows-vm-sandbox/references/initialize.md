# 初始化部署与新建 VM

**当前默认：** 普通实例先走 [基础镜像与实例](images-instances.md)，复用系统，不创建 C 恢复 WIM。本文的 ISO 安装主要用于宿主首次准备和 `template_builder=true` 的专用构建 VM；该 builder 完成第 5 步后转 template.py，不执行第 6 步。下面的基线/重置章节仅用于明确要求兼容旧部署的场景。

本流程适用于空的 Ubuntu/Debian KVM 主机，或在同一宿主创建一台全新 VM。它与已部署实例的日常管理分开。每阶段都写入 `stage/target/evidence/next_action/needs_user_input`；验收不通过就停在该阶段，不猜测、不覆盖、不重复安装。

## 0. 收集输入并形成 profile

先询问并记录：宿主 SSH 地址/身份；公共 `host_root`（如 `/home/vm_run`）、唯一 VM 名称、owner/purpose 标签和 Windows computer name；Windows 版本、语言和 Pro/Enterprise 目标；固定 RAM、vCPU/CPU set、宿主总磁盘预算；RDP/WinRM 发布 IP、端口和允许来源网段；管理员用户名；密码由本机安全提示输入、随机生成后受限保存，还是用户提供受限文件；VM 是否需要 GPU、哪张卡可专供、是否允许共享、宿主现有模型/显示服务是否占用以及可接受的停机/重启影响。GPU 单独填写 `gpu.mode`（`none`、`passthrough`、`vgpu`），不从 CPU 数量推断。新 VM 默认不继承当前 `winvm01` 的 128 GiB、96 vCPU、150 GiB、端口或 `vmadmin`。

生成全新 UUID 和专用 root，复制 `assets/profiles/new-vm.example.json`，先填已知字段。index 要先从介质清单确认，不能为通过校验猜填 3；完整 profile 的校验放在第 2 步。UUID 可用 `python3 -c "import uuid;print(uuid.uuid4())"` 生成。完整校验命令为：

```sh
python3 scripts/profile.py PROFILE.json --new
```

停止条件：缺输入、密码出现在 profile/命令行、GPU 需求未明确、UUID/name/root/bridge/subnet/端口已在 `host_root/registry/<name>.json` 登记、host_limit 小于 guest RAM+2 GiB、start_free 小于 host_limit。publish IP 可以复用，但 RDP/WinRM 端口不能冲突。冲突时询问用户新的 name 或端口，不自动替换。修正 profile 后重新校验。

## 1. 先识别宿主依赖，再交接介质

以下部署命令均在目标 Linux 上以 root 执行。先将本 Skill 整个文件夹（scripts、assets、references）上传到目标机独立工作目录，并进入该文件夹；不能只复制一个 Python 文件，也不能把 Windows 本地 profile 路径直接传给 Linux。先检查：

```sh
cat /etc/os-release
uname -r
test -e /dev/kvm
test -f /sys/fs/cgroup/cgroup.controllers
free -h
df -hT
lscpu
sudo virsh -c qemu:///system list --all
```

缺包按 [镜像与依赖](downloads.md) 在授权范围内安装。生成器针对 Ubuntu 24.04/systemd/cgroup v2/monolithic libvirtd；Debian、modular virtqemud 或不同固件布局先适配，不自动切换共享宿主服务。尚未有完整 profile/index/ISO 时不要先调用 prepare_host。

缺 Windows ISO 或 VirtIO ISO 时停在下载交接，交付官方入口、目标路径和空间要求。按用户要求由用户下载；完成后 agent 可用已有访问权限传到 Linux。不能使用中断文件、猜测 index 或将单 WIM 当可启动 ISO。介质检查：

```sh
sudo python3 scripts/media.py --windows-iso /srv/vm-media/Win11.iso --virtio-iso /srv/vm-media/virtio-win.iso
```

media.py 临时只读挂载并自动卸载，输出 SHA-256、全部 WIM index/名称/edition/架构/语言（ARCH=9 是 x64），验证 NetKVM、vioserial 和 QGA MSI。它不需要 profile/index。按用户指定版本和语言选 Pro/Enterprise，填回 profile。

GPU 选择与直通边界见 [GPU 选择与直通边界](gpu.md)。`gpu.mode=none` 才能走 CPU/软件图形；`passthrough` 先做 PCI/IOMMU/vfio 只读检查；`vgpu` 必须先完成准确型号、官方兼容矩阵、驱动和许可核验，代码返回 `NEEDS_GPU_INTEGRATION` 时停下，不能静默改成 CPU。

## 2. 建立存储和 runtime

profile 完整后重新校验并在目标 Linux root 执行：

```sh
sudo python3 scripts/profile.py /root/vm-profile/PROFILE.json --new
sudo python3 scripts/prepare_host.py --profile /root/vm-profile/PROFILE.json --stage check
sudo python3 scripts/prepare_host.py --profile /root/vm-profile/PROFILE.json --stage storage --apply
```

storage 才创建专用存储预算、runtime 目录、磁盘、EFI/TPM 状态目录、XML/units 和阶段记录；不定义、不启动 VM。验收专用容量、root/vol 余量、loop/discard 策略和所有路径身份；不能只看 guest 报告容量。

check 阶段同时输出 `capacity_plan`：本次要预分配多少 store、分配后宿主根盘还剩多少、同宿主每台已有实例的守卫阈值，以及 `instances_at_risk`（会被压到暂停线以下的实例）。列表非空时 check 以 `NEEDS_USER_INPUT` 阻断，此时先腾空间或与 owner 确认后下调对方阈值，不要继续分配。

## 3. 生成安装答案，不启动

```sh
sudo python3 scripts/prepare_host.py --profile /root/vm-profile/PROFILE.json --stage seed --apply
```

seed 只生成含私密答案的安装 ISO，不能启动 VM、不能 define XML。密码仅进入受限临时介质和 root-only 文件。RunSynchronous command path 上限 259 字符，必须用短 `bootstrap.cmd` 调起长脚本。完成后检查文件存在、权限和引用，不输出答案内容。

## 4. 建立隔离、define 并启动安装

```sh
sudo python3 scripts/prepare_host.py --profile /root/vm-profile/PROFILE.json --stage activate --apply
```

activate 先建立网络隔离、宿主 cgroup、磁盘守卫、日志/计数器和来源白名单，再 define 安装 XML 并 start。它默认拒绝 guest 主动连公网、宿主和其他内网，只发布 profile 的 RDP/WinRM 端口；运行中低内存只告警，冷启动仍需满足 `start_free_gib`。安装启动后等待 Windows 无人值守完成，不要立即 finish。

装机期间 guest agent 还没有应答，`guest_status.py` 会连续返回 `not_ready`——这是正常等待，不是失败。阶段细节与状态语义见 [实例生命周期](instance-lifecycle.md)。

安装阶段验收：UEFI/Secure Boot/TPM 可见；安装器识别 SATA 系统盘；Windows 进入指定语言的 Pro/Enterprise；Windows 内 RAM/vCPU 与 profile 一致；NIC、RDP、WinRM、QGA 和 vioserial 都可用。PnPUtil 的 259 不必单独当失败，但 NIC/QGA 必须实际验证；3010 必须重启再验收。

通过可信宿主 QGA 读取 readiness 和证书指纹：

```sh
sudo python3 scripts/guest_status.py --profile /root/vm-profile/PROFILE.json
```

应有 `provision_success=true`、指定电脑名/管理员、预期 RAM/vCPU、服务 Running 和 HTTPS thumbprint。安装中返回 `not_ready` 不应触发重新安装；`not_running` 是域没运行，`unknown_instance` 是域没定义，`blocked` 才是要修的前置条件。将 QGA 返回的指纹填入控制端 profile，按 [Windows 操作](windows-agent.md) 导入凭据并认证；不从未知端点首次证书响应盲目信任。WinRM 验收身份/上传哈希，RDP 验收真实桌面。3010 必须重启后复验；用户名用 `COMPUTER\user`。WinRM 应 Automatic、`DelayedAutoStart=0`，仅 HTTPS。

时钟要通过 QGA/guest 验证；时钟错乱会使 RDP/WinRM 证书和登录看似失败。网络放行只按目标 IP+端口增加，不能为方便打开整个网段。低内存守卫运行中只告警，暂停不会释放 RSS；冷启动仍需满足 `start_free_gib`。磁盘方向不同：根盘余量低于守卫阈值时实例会被暂停，因此同一宿主上并行分配实例前先看 `capacity_plan`。

## 5. 清理安装残留并完成运行定义

仅在 QGA、WinRM、RDP、网络和资源验收通过后执行：

```sh
sudo python3 scripts/prepare_host.py --profile /root/vm-profile/PROFILE.json --stage finish --apply
```

finish 清除自动登录、答案文件、安装脚本和含密码的临时资料，停止 VM，define 最终无 CD/seed 的 XML，删除 seed，并按最终定义重新 start。它不启用顶层基础脚本（storage 阶段已创建 runtime/units）。验收最终 XML 无 CD/seed、VM 回到可管理运行状态、RDP/WinRM 仍可用、私密临时文件已按引用检查清理。

## 6. 建立初始 C 基线并启用重置

先完成安装后清理临时介质、自动登录和安装密码，再执行：

```sh
sudo python3 scripts/finalize_baseline.py --profile /root/vm-profile/PROFILE.json --capture
```

capture 先估算停机后资源窗口；实际重启仍检查冷启动门槛。它受控停机/重启，记录 C WIM/NTFS、GPT/EFI、D（若已建立）的身份和重置任务，保留 TPM/NVRAM，不自动制造 D。基线只含用户批准的系统、驱动和管理工具，不含临时答案/自动登录/调试文件。

只有用户明确要求并理解 C 会清空、D 会保留时，才执行实际演练：

```sh
sudo python3 scripts/finalize_baseline.py --profile /root/vm-profile/PROFILE.json --accept-reset --discard
```

验收必须包含 C 恢复成功、D GUID/大小/64 位 NTFS serial 和 sentinel hash 保持、RDP 实际桌面会话、WinRM 身份/上传哈希、出站请求被宿主 DROP 且日志有记录。加密、EFS、未知分区或 GPT/serial 证据不完整时 fail-closed，不永久关闭加密。

## 7. 失败恢复

中途失败时保留阶段 JSON 和引用证据；清理失败安装的中间盘/ISO 前检查 `fuser`、loop、qemu、libvirt 引用，不能动共享镜像、其他 VM 或其他用户服务。新生成器的离线检查通过，不等于新主机安装已现场通过。

- check/media 失败：没有 VM/磁盘写入，补输入/包/完整 ISO 后复验。
- storage 失败：可能已有外层文件、挂载、部分 unit。脚本拒绝覆盖 root；先检查部分状态，禁止自动 mkfs 重试。
- seed 失败：credentials 可能已写入；不得删除后重新生成密码。检查同一密码的 seed 是否完整，只在失败实例清理范围明确时删除。
- activate 失败：可能 units 已启用、定义已存在；按实际状态修复原阶段，不覆盖盘。确认 libvirt prepare hook 实际执行；缺 hook 证据时不交付，也不在共享宿主盲重启 libvirtd。
- finish 失败：按停止/定义/seed 引用实际状态续作，不重新安装。
- capture 已生成基线但重启未验收：先恢复通道，不再次 capture。恢复 marker 不得手删。

activate 会启用 IPv4 forwarding 并将默认关机行为设为 shutdown，避免 managed-save 把 guest RAM 写入根盘。已有其他 VM 且全局策略不匹配时拒绝改动，先评估。原始 ISO、安装 staging、管理代码和 libvirt 系统日志另计宿主余量；临时介质不用后按引用清理。

## 新建第二台 VM 与重建定义

工程内的 `start.sh`、`stop.sh`、`status.sh`、`logs.sh`、`counters.sh`、`reset.sh`、`delete.sh` 和 `recreate.sh` 会在初始化时按 profile 生成到该实例的 root；它们和 `lib/` 一起随工程部署，适合迁移到另一台兼容 Linux 主机。入口不应安装为系统全局命令，也不会携带旧实例凭据或磁盘。

新建第二台必须新 profile、新 UUID、新 root、新 bridge /30、新发布端口、新磁盘预算和用户选择的 Windows 管理员/凭据；按本流程从 check 开始，不能 clone `assets/runtime` 或现有 TPM/NVRAM/磁盘。先在公共 `host_root/registry/<name>.json` 预留身份和端口，再创建存储；登记冲突时停下。建议名称可以是 `win-zs-cad`、`win-ls-dev`，仅作示例，不是自动默认值。publish IP 可与其他 VM 共用，但发布端口必须唯一。

若只是定义丢失而磁盘、TPM、NVRAM 和基线仍完整，使用 `host.py recreate --profile PROFILE.json` 恢复定义，随后 `status` 和实际 RDP/WinRM 验收。重建定义不重装系统、不重置 C、不创建镜像层；任何磁盘或基线不一致都停止转入初始化/恢复流程。

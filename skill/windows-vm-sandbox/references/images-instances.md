# 基础镜像与独立实例

这是新 VM 的默认路径。系统只安装一次；每次开机实例从只读模板物化独立磁盘，再创建自己的账户、密码和管理身份。重建是删除整个实例后重新创建，不再恢复 C 盘，不为实例额外保存 WIM。

每个阶段为什么停、停了以后怎么续作、整条链的容量峰值怎么算，见 [实例生命周期](instance-lifecycle.md)。

## 1. 一次制作基础镜像

在目标 Linux 上使用本 Skill 的完整文件夹，不复制单个 Python 文件。选择专用、可丢弃的 builder：正常 profile 加 `"template_builder": true`，按 [初始化](initialize.md) 完成 ISO 安装、驱动、QGA 和批准软件的准备，到 finish 为止；**不要执行 finalize_baseline 或 reset**。普通同事正在用的 VM 不作为 builder。

```sh
sudo python3 scripts/template.py generalize --profile /root/profiles/builder.json
sudo python3 scripts/template.py generalize --profile /root/profiles/builder.json --apply --discard-builder
sudo python3 scripts/template.py capture --profile /root/profiles/builder.json --image-id win11-pro-v1
sudo python3 scripts/template.py capture --profile /root/profiles/builder.json --image-id win11-pro-v1 --apply
sudo python3 scripts/template.py inspect --manifest /home/vm_run/images/win11-pro-v1/manifest.json
```

`generalize` 检查唯一 builder 自建账户、无其他用户 profile、无加载中的 profile、无 D 等数据卷、空 `C:\Work`、C 未加密、未加域。`--discard-builder` 明确授权清除这台专用 builder 的临时账户和 profile、自动登录、答案文件、旧 listener/证书；随后执行 Sysprep `/generalize /oobe /shutdown`。它不是可逆的日常账户修改。

命令在 builder 写入专用 marker，后续正常 start 会拒绝启动已泛化 builder。Sysprep 失败时留在当前阶段，不手删 marker，不重新启动源盘；读取 Sysprep 日志确定原因。capture 只接受关机源，并从 C 分区离线读取 `Windows/Setup/State/State.ini`，要求 `IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE`；检查 GPT、独立 qcow2、镜像校验后发布只读 `disk.qcow2 + manifest.json`。不会导出 builder 的 TPM/NVRAM、账户凭据或控制器。

模板按版本发布，不能覆盖。模板哈希验证失败时停止创建，不就地修复模板。builder 源磁盘保留在原路径；模板发布不意味着已经在另一台 VM 上完成首次启动验收。

模板首启验收后，用下文 `destroy.py` 释放专用 builder 的磁盘和 CPU 登记，不再启动源盘。builder 也应使用 `host_root/instances/<name>`，便于限定范围内完整清理。

## 2. 反复创建实例

复制 `assets/profiles/base-instance.example.json` 到工程之外，填写资源、GPU、账户、凭据方式及管理入口。指定共享的 `image.manifest`。新 root 统一为 `host_root/instances/<name>`；UUID、MAC、TPM、NVRAM、bridge、/30 和端口逐实例独立。

```sh
sudo python3 scripts/profile.py /root/profiles/alice-dev.json --new
sudo python3 scripts/instance.py create --profile /root/profiles/alice-dev.json
sudo python3 scripts/instance.py create --profile /root/profiles/alice-dev.json --apply
```

只需要基础镜像和 VirtIO ISO，不再复制/挂载 Windows 安装 ISO。`storage_gib` 当前须与基础磁盘 virtual size 一致，磁盘扩容是另一个明确操作。CPU 显式集合不能与其他登记实例重叠；自动选择避开已登记 CPU 组。内存、宿主开销、启动余量与其他任务合并检查。共享宿主仍可能有性能竞争，不能把隔离描述为独占整机。

create 内部完成预检、登记、预算存储、独立系统盘、全新 OVMF variables/swtpm、专用 runtime、seed、隔离、启动与 finish。答案文件离线写入新盘的 Windows Panther 位置，仅包含 specialize/oobeSystem；**没有 windowsPE、清盘或 ImageInstall 段**。每台 Windows 首启生成自身的机器身份和 HTTPS 证书。

新实例只发布 start/stop/status/logs/counters 基础入口；旧式 reset 和仅重注册定义的 delete/recreate 入口不生成，以免与完整删除重开混淆。已有宿主若 catalog 仍为旧版本，先按 operate 的受控运行时升级完成检查，不覆盖未知 catalog 文件。

阶段 journal 位于 `host_root/operations/create-<id>.json`。启动或认证超时保留实例；用同一原始 profile 与 `--apply --resume JOURNAL` 续作。不重复创建磁盘，不把 timeout 当成允许清盘。storage 阶段中断时停止自动续作，先检查部分挂载和已分配存储。create 返回 `blocked` 时输出会带 `journal`、`stage`、`domstate` 和可直接执行的 `resume` 命令；`domstate` 为 running 说明实例已经活着，按 resume 续作，不要重跑 create。

create 验证 Windows 名称/CPU/内存/唯一账户、服务、Windows 内 localhost WinRM 新账户认证和上传哈希，再从宿主验证已发布入口的 TLS pin。随后用 `credentials.ps1 import-linux` 和 `guest.ps1 inspect/run` 做控制端真实认证；RDP 桌面、双向实例网络阻断另外实测。

## 3. 删除与重建

```sh
sudo python3 scripts/destroy.py --profile /root/profiles/alice-dev-current.json
sudo python3 scripts/destroy.py --profile /root/profiles/alice-dev-current.json --apply --discard
sudo python3 scripts/instance.py rebuild --profile /root/profiles/alice-dev-current.json
sudo python3 scripts/instance.py rebuild --profile /root/profiles/alice-dev-current.json --apply --discard
```

rebuild 默认沿用实例规格和用户名，生成新 UUID、新密码与新磁盘状态。需要更换用户、资源或密码方式，使用 `--replacement-profile /root/profiles/alice-dev-new.json`。替代 profile 必须为相同 name/root/host_root、新 UUID，并明确选择 generate/file/prompt 凭据方式；不能复用 `existing`。

删除清除这一实例的磁盘、数据、TPM/NVRAM、网络、units 和登记记录。它先验证身份、引用和挂载，正常关机，失败不强制断电。共享镜像、catalog 和其他实例保留；操作 journal 放在被删 root 外。新实例创建失败不会让已丢弃的数据恢复，按保存的替代 profile/journal 续作。

旧式 `/home/vm_run` 同时是实例和共享根，通用 destroy 明确阻断；不能递归清除它来迁移。现有 VM 的平稳迁移要另行明确数据范围。

## 验收与依据

模拟测试覆盖 schema、只读镜像、哈希、独立磁盘、不同 profile 的 XML/路径、无清盘答案、事务失败和限定删除。交付“多实例已现场通过”之前，需从同一模板真实创建两台，比较 SID/主机名/MAC/TPM/证书；验证各自账户登录、RDP 桌面、WinRM 上传、双向网络阻断，并删除重开其中一台，证明另一台和模板保持完整。现有个人 VM 的历史验收不能替代此项。

依据：[Microsoft Sysprep generalize](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/sysprep-command-line-options?view=windows-11)、[specialize](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/specialize?view=windows-11)、[Windows Setup answer files](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/windows-setup-automation-overview?view=windows-11)、[libvirt TPM/NVRAM](https://libvirt.org/formatdomain.html)、[QEMU disk images](https://qemu.readthedocs.io/en/master/system/images.html)。

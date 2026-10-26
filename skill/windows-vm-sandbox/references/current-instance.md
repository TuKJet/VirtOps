# 当前已验收案例：winvm01

本页只描述当前服务器上曾完成现场验收的 `winvm01`，用于排障和理解 profile 字段。它不是新 VM 的模板，也不含密码、私钥或凭据文件。新建实例必须按 initialize 流程重新询问和验收。

`guest_status.py` 的 `provision_success` 是初装时 Windows Temp 中的临时标记。旧实例清理或 C 重置后可为 false；以实际服务、认证、资源与恢复证据为准，不能据此重新安装。

## 身份和资源

| 项目 | 已验收值 |
|---|---|
| VM 名称 | `winvm01` |
| UUID | `8cb9f0f6-3d77-4c1c-8d0b-5ab70ac5f201` |
| 实例根目录 | `/home/vm_run` |
| guest RAM | 固定 128 GiB，Windows 内可见 128 GiB |
| 宿主 cgroup 上限 | 140 GiB，含 QEMU 开销，不是物理内存预留 |
| vCPU | 96；固定到 48 个物理核心及其 SMT 线程 |
| 虚拟磁盘预算 | 宿主侧 150 GiB；单盘，Windows 可自行划分 D |
| 冷启动门槛 | host `MemAvailable` 至少 204 GiB，并满足 root/vol 余量 |
| 运行中低内存 | 仅告警，不暂停；暂停不会释放 QEMU RSS |
| 运行中磁盘余量 | 低于 root_free_gib 阈值时**暂停**该实例；阈值取自该实例已安装的守卫，不是 profile 里的旧值 |

守卫的两个方向不一样：内存不足只告警，磁盘余量不足则暂停。所有实例的磁盘阈值都盯着同一个宿主根盘，所以在这台宿主上新分配 store 可能把本实例压到阈值以下并把它暂停。分配新实例前用 `prepare_host --stage check` 的 `capacity_plan` 核对；本实例被暂停后先看 `journalctl -u winvm01-capguard` 的 `reason` 字段。

## 网络和入口

| 项目 | 已验收值 |
|---|---|
| 专用 bridge | `winbr01` |
| guest / bridge | `172.30.61.2/30` / `172.30.61.1/30` |
| 发布地址 | `192.168.20.5` |
| 允许来源 | `192.168.20.0/24`；本机控制入口另有 loopback 隧道 |
| RDP | `192.168.20.5:13389` → guest `3389` |
| WinRM HTTPS | `192.168.20.5:15986` → guest `5986` |
| guest 主动出站 | 默认拒绝公网、宿主和其他组网地址；DROP 计数与采样日志分开记录 |

RDP 使用 `WINVM01\vmadmin`；页面不保存密码。WinRM 只使用 HTTPS、独立证书和固定 thumbprint；控制端通过 WinRM 上传/执行脚本，工作目录为 `C:\Work`。允许 agent 使用该管理员账户是本案例的明确选择，不应自行增加 `agent` 或 `rdesktop` 账户。

## 已验证能力

- UEFI/Secure Boot/TPM、SATA 系统盘、VirtIO 网卡和 vioserial/QGA 已验证。
- RDP 已建立实际桌面会话；WinRM 已验证管理员身份、文件上传及 SHA-256 回读。
- Windows 内访问 Google、百度、1.1.1.1、8.8.8.8、宿主和其他内网目标均被阻断，宿主专用网络日志记录拦截；WinRM 返回结果本身不是 guest 出站许可。
- C-only reset 已演练：C 恢复基线，D 的 GPT/分区身份、大小、NTFS serial 和测试内容 hash 保持；没有整盘镜像层。
- delete/recreate 的实现只注销/注册定义，保留磁盘、TPM/NVRAM 和 D；本次 Skill 整理未对运行中 VM 执行它们。

## 常用操作

从工程目录按 profile 执行：

```sh
python3 scripts/host.py status --profile winvm01.json
python3 scripts/host.py logs --profile winvm01.json
python3 scripts/host.py counters --profile winvm01.json
python3 scripts/host.py start --profile winvm01.json
python3 scripts/host.py stop --profile winvm01.json
```

RDP/WinRM 认证失败先核对 `COMPUTER\user`、VM 状态、guest 时间、WinRM HTTPS thumbprint 和实际服务；端口响应不等于登录成功。宿主出现 OOM 时先读取 status、生命周期日志、内核/libvirt 日志和其他用户进程，不能自动停 Qwen 或其他用户任务，也不能擅自降低 128 GiB。

## 重要边界

当前案例的端口、UUID、网络、资源和阈值都绑定这一台实例。runtime 资产只能作为经过验收的实现参考；复制到新 VM 前必须重新生成身份、磁盘、网络和账户配置。本文不证明任何新的空 Linux 主机已经部署成功，也不授权对当前 VM 进行 reset、delete、recreate、stop 或重建操作。

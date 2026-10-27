# 按证据排错，不重装碰运气

先确定是 SSH、libvirt、Windows 安装、RDP 认证、WinRM 还是网络守卫。只对当前失败阶段做一处修正并重验；同一原因修正两次仍失败时，交接原始错误、阶段和下一条检查命令。不要连续敲键、反引号或批量重跑安装脚本。实际安装限制见 [踩坑清单](installation-pitfalls.md)。

## 先收集这四项

控制端运行 `python scripts/host.py inspect --profile PROFILE.json`、`status`、`logs`、`counters`。SSH 不通时先检查控制端执行器是否能启动，再用已获授权的 SSH 身份、组网/物理路由和 BindAddress 做一次只读连接；连接失败不能归因于 Windows。

| 证据 | 判断与下一步 | 验收 |
|---|---|---|
| `domain.matches_profile=false` | 停止操作，核对 UUID 和根目录；禁止改 profile 去迎合一个未知 VM | 用户目标与远端身份一致 |
| `running` 且服务/磁盘/网络正常 | 转入 Windows 通道检查；无需停机 | 认证并执行检查命令 |
| `paused` | 查看 lifecycle 的具体理由；修正网络、存储或前置条件再用 start | 守卫检查通过并 resume；不能裸 `virsh resume` 绕过 |
| `shut off` 且无用户停机记录 | 看宿主 OOM、QEMU 日志和 memory.events | 解释退出原因并评估可重新启动窗口 |
| recovery marker 存在 | 转 [reset](reset.md)，匹配 context hash/request_id | 正常的恢复后验证删除 marker；不手工删除 |

## 安装器循环报错

1. 记录错误窗口的准确文本、安装 pass 和最后一次动作。不要重复键入长 PowerShell。
2. 核对实际 ISO 中镜像清单、profile `image_index`、x64、语言、Pro/Enterprise。完整 ISO 和 WIM 单文件不能混用。
3. 检查 seed 内 Autounattend 的 `RunSynchronous/Path` 长度。模板用约 115 字符的 `cmd.exe … bootstrap.cmd`；当初 291 字符命令超过微软 259 字符限制，造成同一个报错循环。修复命令长度，长逻辑留在文件中。[微软字段定义](https://learn.microsoft.com/en-us/windows-hardware/customize/desktop/unattend/microsoft-windows-deployment-runsynchronous-runsynchronouscommand-path)
4. 若脚本没有被复制，检查 seed 卷标/`VMSEED`/`bootstrap.cmd` 和虚拟 CD 挂载，不能改写原微软 ISO。这个流程的系统盘是 **SATA**，安装阶段不依赖 VirtIO 存储驱动。
5. 查看私密安装控制台或已可用的 QGA。尚无 Windows/QGA 时 RDP 不会凭空可用。需要按键时，先确认屏幕确实是“Press any key to boot from CD”，再仅对该 VM 发送一次 Enter；不要循环盲发。

宿主临时证据命令（NAME 替换为已验证名字）：

```sh
sudo virsh -c qemu:///system domstate NAME
sudo virsh -c qemu:///system dumpxml NAME
sudo journalctl -u libvirtd --since '-15 min' --no-pager
# 截图可能包含屏幕信息，放受限证据目录；不要提交 Git。
sudo virsh -c qemu:///system screenshot NAME /受限目录/install.ppm --screen 0
# 仅屏幕已确认启动提示时执行一次；安装后不得用来碰运气。
sudo virsh -c qemu:///system send-key NAME KEY_ENTER
```

首次安装失败不是重新格式化现有 VM 的授权。全新且用户已明确无数据的失败实例，先核实定义、磁盘、TPM、loop 和挂载引用，再按用户授权清理重建；不保留无用中间文件，不删除共享原始介质。

## QGA / 驱动未就绪

系统能启动、网卡能用都不等于 QGA 可用。libvirt XML 必须有 `org.qemu.guest_agent.0` 通道；Windows 需要 `vioserial/w11/amd64/vioser.inf` 和 `guest-agent/qemu-ga-x86_64.msi`，并验证 guest-ping。NetKVM 单独负责网络。

PnPUtil 返回 259（没有更多匹配项）可能已存在合适驱动，需查设备及实际功能；3010 需要重启。MSI 返回 0/3010 才允许继续；QGA 请求仍失败时不建立恢复基线。[微软返回码](https://learn.microsoft.com/en-us/windows-hardware/drivers/devtest/pnputil-return-values)

## RDP 无法连接 / 登录失败

先分层检查：VM running → guard/network 正常 → Windows TermService running → 从批准来源访问发布端口 → NLA 认证 → 实际桌面。端口成功或 TLS 握手不能证明登录。

登录填写 `COMPUTER\user`。本例为 `WINVM01\vmadmin`，不是控制电脑名称/域，也不是把 VM 名称当用户名。本次失败事件是错误用户 `kaijie\WINVM01`，4625 的 `SubStatus=0xc0000064` 表示账户不存在；先纠正用户名和客户端 TERMSRV 保存项，不重置密码。[微软 4625 定义](https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-10/security/threat-protection/auditing/event-4625)

只清除该目标 `TERMSRV/IP[:PORT]` 的错误保存项；不清用户全部 Windows 凭据。实际验收查看 RemoteConnectionManager 1149（认证）并用 `quser` 确认桌面 Active，或让用户确认已进入桌面。

若暂停后时间偏差，先比较宿主和 Windows UTC；允许时通过 QGA `guest-set-time` 传入宿主 **Unix 纳秒**。先修时间再判断证书/认证，不为此重置 TPM、NVRAM 或密码。

## RDP 连上后几十秒内断开

和「连不上」是两回事：认证成功、桌面出现、图形管道协商成 `0xA0600`(AVC)，然后会话在几十秒内被拆掉，客户端报原因码 **2308**（`0x904`，套接字已关闭）。

先看客户端 `Microsoft-Windows-TerminalServices-RDPClient/Operational`：

| 事件 | 含义 |
|---|---|
| 1102 | 客户端启动了到服务器的**多传输**（UDP）连接 |
| 1103 | 多传输连接**已建立** |
| 1104 | 多传输连接**无法建立** |
| 1026 | 断开原因码，2308 = 套接字被本地关闭 |

**1102 后紧跟 1104，就是 UDP 多路传输没打通。** Windows 11 客户端协商 `0xA0600` 图形管道后必须使用 UDP 多路传输；UDP 不通时客户端不会退回纯 TCP，而是把整条会话拆掉，所以现象是「刚连上就断」而不是「一直连不上」。

两处都要查，缺一处都还是断：

```sh
# 宿主：发布端口有没有 UDP 的 DNAT 与放行
sudo nft list ruleset | grep -E 'udp dport (13389|你的端口)'
# guest：防火墙有没有放行 UDP 3389
Get-NetFirewallRule -DisplayName 'Remote VM baseline RDP *'
```

修法：宿主把 RDP 端口**同时**发布为 TCP 和 UDP（`PUBLISHED_UDP`），guest 放行 `Remote VM baseline RDP UDP`。WinRM 仍然只走 TCP。

## 大包被静默丢弃（MTU 不对齐）

症状不是报错，而是：连接能建立，但桌面刷新卡顿、大文件传输停滞、SSH 传大包时进度条走到一半不动。

判据是**丢包大小有明确分界**：同一条链路上 `ping -M do -s 1300` 通、`-s 1400` 100% 丢。

```sh
ping -M do -s 1400 PEER     # 丢
ping -M do -s 1300 PEER     # 通
```

原因是 guest 没有默认网关，TCP 的 MSS 由 guest 网卡 MTU 决定；桥和网卡还是 1500 时，guest 会协商出承载 publish_ip 的传输层装不下的报文段，这个报文段被静默丢弃、不回 ICMP。

修法见 [profile.md](profile.md) 的「发布地址的 MTU」：`network.mtu` 填承载 publish_ip 的那条传输的 MTU，桥和 guest 网卡一起对齐。**只改一边仍然丢包。**

## WinRM 无法执行

- 验证 HTTPS 5986 listener、证书实际 thumbprint、`WinRM` 为 Automatic 且 `DelayedAutoStart=0`。仅检查服务会漏掉延迟自动启动问题。
- 使用标准 `Microsoft.PowerShell` 管理员端点；用户选择同一 admin 给 AI 使用，不另建 agent 账户。
- 控制端必须先从可信宿主/QGA 获取证书指纹，再用 `guest.ps1` 固定。证书变更先核对重装/续期事实，不把错误证书直接写回 profile。
- `Restricted` 下直接 `& script.ps1` 被拒绝时，使用工具已提供的 **子进程** `-ExecutionPolicy RemoteSigned`。初始化与 SYSTEM 恢复任务的 `Bypass` 也是一次进程参数；不得永久 `Set-ExecutionPolicy Unrestricted` 或修改全局策略。
- 不退回 HTTP、Basic、未加密或 `TrustedHosts=*`。DPAPI 凭据只能由创建它的 Windows 用户/机器解密；换控制端需安全重建句柄。

## 组网端口通一阵断一阵

EasyTier 在本例将组网连接转成宿主 `OUTPUT` 上的 `192.168.20.5 → 192.168.20.5` 本地连接。只写 PREROUTING DNAT 会漏掉它；只有 `127.0.0.1` 的 OUTPUT 规则也不够。检查生成的 `LOCAL_ENDPOINTS`、私有链、SNAT 和 conntrack 响应规则，保留 loopback 与发布 IP 的本地转换。用**批准的组网控制电脑**完成真实认证，不能以宿主 127 测试代替远端验收。

Windows 无默认网关/DNS，因此“域名请求失败”可能只证明 DNS 不可用。网络验收需同时做直达 IP 的 TCP/UDP 测试并匹配宿主 DROP source/destination/port/counter；必要时仅短暂增加精确 /32 route 来让探测包到达宿主，记录并清除该测试对象，不给全局默认网关或代理。日志会限速采样，DROP 计数覆盖全部包，日志不能承诺每包一行。它也不能从加密连接还原 URL 或进程名。

## OOM / 存储门槛

用 `journalctl -k` 的 Out of memory/Killed process、QEMU cgroup `memory.events` 和其他用户进程 RSS 判断是全局 OOM 还是实例触顶；两者不同。128 GiB 是 guest 固定内存，140 GiB 是本例宿主最大值，都不为其他任务保留剩余 RAM。暂停保留 RSS。运行中低内存只告警；本例冷启动 204 GiB 门槛保留。不要为恢复 VM 自动停同事任务或取消保护。[内核 cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)

存储检查 exact mount、外层文件 size/blocks/inode、FS UUID、loop discard queue=0、TPM bind 与 qcow backing chain。无 exact mount 时先修挂载，不向裸 `/home/.../vol` 写盘；禁止稀疏外层文件、额外镜像层、managed-save 内存文件、全局 fstrim 改动。WIM、qcow、TPM/NVRAM 和 VM 网络日志共用该总预算，Windows 显示的虚拟容量并非扣除基线后的可用容量。

## 给下一位 agent 的失败交接

只交接 profile 路径、stage、UUID、失败命令类别、精简错误、已做修正、验收缺项、下一条只读检查和需要用户回答的字段。密码、seed 内容、原始凭据输出不进入交接。恢复/安装失败保留必需的 request/context/marker 证据，成功后只清理已经不被引用的本次临时文件。

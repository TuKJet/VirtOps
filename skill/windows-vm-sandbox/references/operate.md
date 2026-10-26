# 日常管理

新模板实例的完整删除/重开使用 [基础镜像与实例](images-instances.md) 的 `destroy.py` / `instance.py rebuild`；资源和保留系统改账户使用 [维护](configure.md) 的内置命令。本文 `host.py delete/recreate` 仍只注销/重注册定义，属于兼容入口，**不是完整删除或干净重建**。

需要换同事时不要把 `delete/recreate` 当作重建 Windows。先读 [换使用者并复用 Windows 基线](reprovision.md)；`delete/recreate` 只恢复 libvirt 定义。

日常管理使用 profile 驱动的 `scripts/host.py`。它通过 profile 的 SSH 身份调用目标机 root-owned controller，不把宿主管理命令复制成一堆新入口，也不改系统全局命令。先确认 profile 的 name、UUID、root 和 SSH 路由与远端事实一致：

旧实例要更新运行脚本时，先执行 `upgrade_runtime.py --profile PROFILE.json` 做只读检查；用户已授权升级且检查返回 ready 后加 `--apply`，不重复询问。升级器必须验证 store inode/文件系统 UUID、live XML 的 name/UUID/system disk/NVRAM/TPM、profile、manifest 和安全目录祖先，并在 apply 后只读验证 network guard 与 `vmctl status`。它不重新安装、不重置、不改 VM XML、防火墙或 systemd 单元，也不删除未知文件。若采集器代码变化且其服务正在运行，仅重启该实例的采集器使新规则生效；未运行的采集器不自动启动。结果中 `collector_restarted` 与 VM 的 `restarted` 分开报告。

```sh
python3 scripts/host.py inspect --profile PROFILE.json
```

`inspect` 是只读操作，检查 libvirt 定义、UUID、资源、存储挂载、工具、监听端口、当前 VM 列表和门槛。输出 `matches_profile=false`、同名但 UUID 不同、root 未挂载或存在 recovery marker 时，停止，不执行生命周期命令。

## 用户可见的八个入口

远端 `/home/vm_run/scripts` 顶层只保留基础命令：`start.sh`、`stop.sh`、`status.sh`、`logs.sh`、`counters.sh`、`reset.sh`、`delete.sh`、`recreate.sh`。底层 Python、网络、QGA、恢复和 guard 放在 `scripts/lib`，用户不直接调用。远程 shell 入口等价于：

```sh
/home/vm_run/scripts/start.sh
/home/vm_run/scripts/stop.sh
/home/vm_run/scripts/status.sh
/home/vm_run/scripts/logs.sh
/home/vm_run/scripts/counters.sh
/home/vm_run/scripts/reset.sh --discard
/home/vm_run/scripts/delete.sh
/home/vm_run/scripts/recreate.sh
```

从本机 agent 使用时统一走：

```sh
python3 scripts/host.py start --profile PROFILE.json
python3 scripts/host.py stop --profile PROFILE.json
python3 scripts/host.py status --profile PROFILE.json
python3 scripts/host.py logs --profile PROFILE.json
python3 scripts/host.py counters --profile PROFILE.json
python3 scripts/host.py delete --profile PROFILE.json
python3 scripts/host.py recreate --profile PROFILE.json
```

每条命令读取 JSON 结果。不要靠“命令返回了”判断成功；关注 `status`、`actual_state`、UUID、cgroup、store、服务和下一步字段。

同一宿主有多台 VM 时，使用生成到公共 `host_root/scripts/status.sh --all` 的目录视图查看 `host_root/registry/<name>.json`。它只输出每台已登记 VM 的 name、labels.owner、labels.purpose、RDP `IP:port`、WinRM HTTPS URI、Windows `admin_user`、RAM/vCPU/host limit 和运行状态，不输出密码、凭据路径或秘密。登记表包含尚未启动的 VM，因此不能只用 `virsh list` 判断全局资源占用。

每台实例仍有自己的 `root/scripts/status.sh`。公共 `status --all` 只是只读目录，不替代单实例 `inspect`。若登记表缺失、重复 name/UUID/root/bridge/subnet/端口或 profile 与登记不符，停止启动并先修正登记；不自动删除登记、不自动选择替代端口。

这些基础入口随工程在初始化时生成到目标 root 的 `scripts/`。它们只依赖该实例的 profile 和运行时身份，不写死 `/home/vm_run`、IP、端口或 UUID；复制工程本身不会创建系统全局命令，也不会迁移已有 VM 数据。

## 启停和防止全局 OOM

`start` 是幂等的：运行中会检查并返回状态；关机时先检查磁盘、store、网络、TPM、资源和 profile UUID，再按冷启动门槛启动。`start_free_gib` 是按该宿主预算计算的门槛，当前案例是 204 GiB；门槛不足时命令应拒绝启动，不要自动降低 guest RAM、停止其他用户任务或绕过检查。

`stop` 请求 guest 正常关机并等待最多 120 秒；超时只暂停并报告，不强制断电，以免损坏 C/D。低内存运行中只记录告警，不暂停 VM，因为暂停不会释放 QEMU RSS。发生宿主 OOM 时先查 `status`、内核/libvirt 日志和资源消耗，不能把“剩余内存低”误当成 VM 自身超限。

磁盘方向相反：宿主根盘余量低于本实例的 `root_free_gib` 阈值时，容量守卫会**暂停**该实例；网络或存储边界失效同样会暂停。由于所有实例的阈值都盯着同一个根盘，在同一宿主上新分配 store 可能把别的实例压到阈值以下并把它暂停。分配前用 `prepare_host --stage check` 的 `capacity_plan` 或 `status --all` 核对；发现他人实例被暂停时，先腾出空间，或与 owner 确认后用 `configure` 下调其 `root_free_gib`，不要为了新实例擅自改别人的阈值，也不要自动 resume 一台正被别人使用的实例。

多人使用前结合 `status --all`、`inspect` 与单实例 `status`：目录视图看所有登记 VM 及标签，inspect 看 UUID、guest RAM/vCPU、marker，status 看状态、cgroup 上限/CPU、store、root/vol 余量和 guard。Windows listener/认证另用 guest 工具验证；NAT 发布端口通常不在宿主 ss 列表中。启动任意新 VM 前还需做同宿主总预算检查；各 VM 的上限不是物理内存预留，也不能替代宿主全局 OOM admission。

## 日志和网络计数

用户要看 VM 内部主动对外通信时，使用网络专用视图：

```sh
python3 scripts/host.py logs --profile PROFILE.json --network-only
python3 scripts/host.py counters --profile PROFILE.json
```

网关先按策略 DROP 未批准的连接；采集器从内核日志记录被阻断的尝试，不会保留或放行连接，也不负责结束 Windows 进程。默认只允许批准 CIDR 进入 RDP/WinRM；guest 主动向公网、宿主和内网新建连接被拒绝。需要 Git/API 时按 IP+端口批准并记录变更。

每个实例的采集器由 profile 渲染其 bridge、子网、网关与日志前缀。按 bridge 入口和 DROP 方向识别 guest 发起的单播尝试，即使 guest 伪造源 IP 也不漏掉；不采集宿主输出、入站管理访问、广播/组播及发往网关的 RDP/WinRM 回复。使用管理源端口的新 TCP SYN 仍记录。记录仅含时间、方向、IP、端口、协议、TCP 标志和阻断结果等元数据，不含报文内容或进程名。采集器重启后仅跟随新记录，避免重放内核日志末尾。

`logs` 默认还返回生命周期事件；`--network-only` 仅返回 `network_samples`，省略生命周期和宿主文件路径。该视图不改写已有日志，采集过滤规则只影响之后写入的记录。恢复结果另在 recovery JSON。内核 LOG 规则按链限速为每分钟 10 条、突发 20 条；`counters` 是完整规则命中包计数，既不是连接数，也不能用采样日志推断全部连接尝试。

真实验收仍需端到端：RDP 认证并建立桌面、WinRM 使用选定管理员上传并校验哈希、guest 内对 Google/百度等出站请求失败且宿主日志有 DROP。仅端口监听或一次超时不构成通过。

## 删除与重建定义

`delete` 只删除 libvirt 定义和相关运行注册，不删除 store、虚拟磁盘、TPM、NVRAM、D 数据或恢复基线；运行中必须先正常 stop。`recreate` 读取目标 vol 内保存的 XML，核对 name/UUID/磁盘/TPM/NVRAM 身份后重新注册定义；成功后仍需 `status`，再由用户决定 `start`。这两个动作都不是 Windows 重装或 C-only reset。

如果实际磁盘、TPM、NVRAM、profile UUID 或基线不一致，禁止 recreate，转回初始化/恢复流程。失败中间物只在确认无 qemu/libvirt、loop、mount、fuser 引用后清理。

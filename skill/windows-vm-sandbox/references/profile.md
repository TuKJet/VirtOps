# Profile 参数档案

新实例优先复制 `assets/profiles/base-instance.example.json`，填 `image.kind=base` 与只读 `image.manifest`。这种模式不需要 Windows ISO 或 image_index，仍需要 VirtIO ISO；root 统一为 `host_root/instances/<name>`，storage_gib 与模板 virtual size 一致。`image.kind=installer`（省略时兼容旧格式）仅供从 ISO 安装，专用构建 VM 可显式设置 `template_builder=true`。新实例不建立 C 恢复基线，详见 [基础镜像与实例](images-instances.md)。

Profile 是一次 VM 的唯一配置输入。它描述目标、资源、网络和凭据句柄；profile 本身不保存密码。先复制 `assets/profiles/new-vm.example.json`，补齐后运行：

```sh
python3 scripts/profile.py PROFILE.json --new
```

校验失败时不要猜值，也不要把已部署实例的值当默认值。`NEEDS_USER_INPUT: field` 就是停下询问用户该字段。

## 必填选择

| 区域 | 字段 | 含义与规则 |
|---|---|---|
| `name`/`uuid`/`host_root`/`root` | 实例名、全局 UUID、宿主公共根和实例目录 | name 为 2–12 位小写 ASCII，必须在同一宿主唯一，并生成唯一的大写 Windows computer name；新建 UUID 必须全新。`host_root` 是同一宿主所有 VM 共用的绝对根（示例 `/home/vm_run`），新实例推荐 `root=HOST_ROOT/instances/<name>`；当前 `/home/vm_run` 作为 legacy root 继续支持。 |
| `labels` | owner、purpose | 新建时填写的公开目录标签，用于 `status --all` 识别使用者和用途；旧实例可省略。不能当作权限或安全边界，不得放密码、token 或敏感信息。 |
| `ssh` | `host`、`user`、`port`、`identity_file`、`bind_address` | 宿主管理入口。优先让用户指定物理或组网地址；先 `inspect`，不把 SSH 失败当作安装失败。 |
| `resources` | `memory_gib`、`host_limit_gib`、`vcpus`、`storage_gib`、`cpu_set` | Windows 看到的 RAM 固定为 memory；host_limit 至少比 guest 多 2 GiB，给 QEMU 开销；storage 是宿主总预算，创建后不得扩散。CPU set 若填写必须正好匹配 vcpus。GPU 不包含在 CPU 自动分配内，需单独填写 `gpu`。 |
| `network` | bridge、/30、publish_ip、client_cidr、RDP/WinRM 端口、mtu | 每台 VM 使用独立 /30 和唯一发布端口。client_cidr 是可进入的来源网段。端口必须不同，不能默认为 13389/15986；端口由用户选择，冲突时拒绝，不替用户自动换端口。`mtu` 可选，默认 1500；见下方「发布地址的 MTU」。 |
| `windows` | computer_name、admin_user、language、image_index | admin_user 是用户明确选择的管理员账户，供 AI 与桌面共用；不要擅自创建 agent/rdesktop。image_index 必须从 ISO 的实际清单中选 Pro/Enterprise 和语言/架构匹配项。 |
| `credentials` | mode、secret_file、local_clixml | 新建只能 `generate`、`prompt` 或 `file`；existing 仅适用于已存在且已验收的实例。profile 只放凭据路径/句柄；实际私密 `credentials.json` 或 DPAPI CliXML 可在目标 Linux/本机以 0600 保存，不能进入 Git、日志或命令行。 |
| `media` | windows_iso、virtio_iso | 指向已下载的本地/宿主 ISO；缺失时进入下载交接，不自动拿未知镜像代替。 |
| `gpu` | mode、pci_devices、approval | mode 必选 `none`/`passthrough`/`vgpu`。直通必须列出管理员明确专供、已绑定 vfio-pci 且 IOMMU group 完整的 PCI 地址；vgpu 只在准确型号、官方兼容/驱动/许可/profile 核验后进入独立集成分支。 |
| `limits` | root_free_gib、start_free_gib、resume_free_gib、watch_free_gib | 磁盘和内存保护阈值，必须结合实际主机容量和其他任务预算向用户展示后确定。start_free 是冷启动门槛，必须覆盖 host_limit；运行中 watch 只做告警，不暂停 VM。新机可从 32/16/16 GiB（root/start 仍需按资源重新算）开始评估；当前 winvm01 的 250/204/80/64 只是现场案例。 |

## 发布地址的 MTU

`network.mtu` 描述的是**承载 publish_ip 的那条传输**的 MTU，不是桥或网卡的默认值。专用桥和 guest 网卡会被对齐到这个值。

原因：guest 没有默认网关，TCP 的 MSS 由它自己的网卡 MTU 决定。如果桥还是 1500 而传输层只跑得动 1360，guest 会协商出一个传输层装不下的报文段，这个报文段被**静默丢弃**、不回 ICMP，表现是连接建立后随机卡死或大文件传输停滞，而不是明确的报错。

取值方法：先在宿主上看承载 publish_ip 的接口 MTU，再填同样的值。

```sh
ip -4 addr show                 # 找 publish_ip 所在的那条隧道/网卡
ping -M do -s 1400 PEER        # 1400 丢包而 1300 通 = MTU 低于 1400
```

典型取值：直连局域网 1500；EasyTier/WireGuard 等组网隧道取隧道接口自身的 MTU（常见 1360/1380）。**不要凭印象填**，也不要为了绕过丢包而把 guest MTU 调大——那只会让丢包更晚出现。

已经部署的实例改这个值需要重建桥（`ip link set dev BRIDGE mtu N`）并同步 guest 网卡（`Set-CimInstance MSFT_NetIPInterface` + 注册表 `MTU`），两者必须一起改；只改一边仍然会丢包。

## 资源口径

`memory_gib` 是 Windows 内可见的固定内存；`host_limit_gib` 是该 VM cgroup 的最大宿主占用，不能当作物理内存预留。宿主仍须给系统、模型服务和其他用户留下预算。暂停不会释放 QEMU 已占用的 RSS，因此低内存不能靠暂停解决；守卫只记录告警，网络/存储边界失效才停止实例。冷启动仍需满足按实际容量计算的 `start_free_gib`；当前案例是 204 GiB，这个门槛不可被小模型擅自取消，也不可把它误套到所有新主机。
GPU 选择独立于 CPU/RAM：`none` 使用 CPU/软件图形；整卡 passthrough 会让宿主失去该卡，不能与宿主 Qwen 同时使用；vgpu 不是自动共享开关，必须经过型号、官方兼容矩阵、驱动和许可核验。

## 现有案例与新实例的边界

`winvm01.example.json` 仅用于理解当前已验收实例（128 GiB、96 vCPU、150 GiB、13389/15986、vmadmin）。它不是新 VM 的默认 profile。新建必须重新生成 UUID、磁盘/网络身份、端口、资源和 Windows 账户；runtime 资产不能原样 clone。

## 停止点

缺任何资源、端口、网段、ISO、Pro index、GPU 需求/专供卡/共享意愿或凭据提供方式时停止并列出字段。用户可以先下载 ISO，再带回 Linux 文件路径继续；不要把密码要求用户粘贴到聊天中。`media.py` 输出的 index 清单是选择依据，不能默认使用 3。当前 `winvm01` 的 `gpu.mode=none` 只是本案例事实，不是新机默认结论。

## 换使用者 profile

复用已安装 VM 时不要复制完整的新 VM profile。使用 `assets/profiles/owner-rebuild.example.json` 作为 owner profile，只填写现有实例的 `name`/`uuid`、新管理员用户名、凭据句柄和 `data_policy`。它不改变 RAM、vCPU、GPU、网络或端口；这些字段若要改变，必须另走新 profile 的迁移/重建流程。`data_policy=clear` 只有在命令同时带 `--discard-data` 时才生效。

## 同宿主登记

初始化前先读写 `host_root/registry/<name>.json`（目录和登记文件由 root 创建，目录默认 0755、登记内容 root 可写），登记 name、UUID、root、bridge、subnet、RDP/WinRM 端口，即使 VM 还未启动也要占位。publish IP 可以共享，但 RDP/WinRM 端口必须唯一。新 profile 与登记表冲突时必须返回 `NAME_OR_PORT_CONFLICT` 或 `PORT_CONFLICT` 并停下，让用户选择新 name/端口；小模型不得自动猜端口或改 root。公共目录只保存展示所需字段，不保存密码或凭据路径。多台 VM 同时运行还要把各 VM 的 guest RAM、host_limit、磁盘预算和宿主已有任务合并核算；单台 cgroup 上限不能保证整机不 OOM。

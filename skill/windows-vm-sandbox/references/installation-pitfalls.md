# 安装和恢复踩坑清单

这是给小模型的硬规则。遇到同一错误不要反复改模板；先保留证据、回到对应阶段，最多做两次同因修复，仍失败就停下交接。

## Windows 安装

- **OVMF 等按键引导**：OVMF 把 CD-ROM 当作可移动介质，装机前会停在 "Press any key to boot from CD/DVD"；没人按键就一直黑屏，最后只剩 `BdsDxe: No bootable option or device was found.`。用 `virsh send-key NAME KEY_ENTER` 周期发回车直到装机开始；只有挂 Windows 安装 ISO 的全新安装路径需要，模板派生实例从自己的 EFI 分区启动。
- **windowsPE 缺语言页**：unattend 的 `windowsPE` 段没有 `Microsoft-Windows-International-Core` 时，Windows Setup 会强制显示"选择语言"和"选择键盘布局"两页，无人值守装机就停在这一步。该组件必须与 `Microsoft-Windows-Setup` 平级，不能塞进它内部，否则是非法嵌套。
- **OOBE 留下 defaultuser0**：Windows 11 的 OOBE 有时留下 `defaultuser0` 占位账户和 `C:\Users\defaultuser0`，模板预检会因此报 `BUILDER_OTHER_ACCOUNTS` 或 `BUILDER_PROFILE_IN_USE_OR_FOREIGN`。在 guest 侧删掉这个占位账户及其 profile，不要放宽预检。
- **TPM 自动开设备加密**：带 TPM 的 Windows 11 装完会自己启用 Device Encryption，可能停在"几乎全部加密、却没有密钥保护"的状态，模板预检报 `BUILDER_DISK_ENCRYPTED`，半加密的盘还会被烤进基础镜像。guest 侧要写 `AllowDeviceEncryption=0` 与 `DisableDeviceEncryption=1` 策略，并对已经加密的卷执行 `manage-bde -off C:` 等它回到 `FullyDecrypted`。
- **RunSynchronous 路径过长**：unattend 的 command path 上限 259 字符。用短 `bootstrap.cmd` 调起脚本，缩短工作根目录；不要把长 PowerShell 路径直接拼接进 XML。
- **WIM index 猜错**：index 3 不是通用事实。用 `wimlib-imagex info`/安装器清单核对名称、Pro/Enterprise、x64 和语言；不匹配就停下。
- **PnPUtil 259/3010**：259 表示没有更多匹配项，不单独判失败；必须实际检查 NIC、QGA。3010 表示需要重启，重启后再验证。
- **vioserial 漏装**：存储和网卡能用不代表 QGA 能用。vioserial 是 QGA/时钟/恢复链路的必要驱动，必须安装并实际请求成功。
- **WinRM 启动慢**：服务应为 Automatic 且 `DelayedAutoStart=0`；重启后检查 HTTPS listener、证书 thumbprint 和身份，不以端口监听代替认证。
- **RDP 用户名错**：使用 `COMPUTER\user`（例如 `WINVM01\vmadmin`）。0xc0000064 多为账户前缀/名称错误，不要先重置密码。
- **时钟错误**：暂停或 QGA 未同步会造成 TLS/登录假失败；先查 guest/host 时间，再改证书或密码。

## 账户与密码

- **管理员替别人改密码要用 SetPassword**：WinNT ADSI 提供者的 `ChangePassword` 只允许账户修改自己的密码，管理员改别人的密码会返回"拒绝访问"。管理员路径改用 `SetPassword`（不需要旧密码），skill 的 `account.py` 随后会把 `credentials.json`、`owner.json`、`instance.json` 和 registry 一并收尾。
- **改名与改密码是两条路**：`account.py` 的 guest 助手在目标名与当前名相同时跳过 `Rename-LocalUser`，只做改密；因此 `--admin-user` 传同一个名字就是一次纯改密事务。
- **resume 比对的是请求身份**：preflight 会改写 `resources`（显式 CPU 选择变成排序后的逗号列表，并补 `cpu_order`/`threads_per_core`/`amd`），journal 里存的是规范化之后的 profile。逐字段比对会把同一个请求判成"另一个实例请求"；CPU 集合要按 id 集合比，其余字段逐字段比。
- **续作不要重新生成密码**：事务失败时密码留在服务器的 `account-pending.json`，`--resume` 会复用它；重新生成会让续作与 guest 阶段记录的密码对不上。

## 宿主资源、磁盘和网络

- guest RAM 是 Windows 固定可见内存；host cgroup 上限还需 QEMU 开销。上限不是预留，暂停也不会释放 QEMU RSS。运行中低内存只告警，不因剩余内存低而杀/暂停 VM；冷启动的 204 GiB 之类门槛仍必须遵守。
- 150 GiB 是宿主总存储预算，不只是 Windows C 的逻辑容量。loop 存储关闭 discard/配置外层分配守卫，确认 qcow/raw、loop 和根盘的实际占用；不要靠 `df` 看到 guest 150 GiB 就认定宿主不会增长。
- 只有批准来源网段可进入 RDP/WinRM；guest 出站默认拒绝公网、宿主和其他组网 IP。控制端通过 WinRM 传结果不等于允许 VM 自主联网；若控制端代为下载，属于控制端 agent 的权限边界，需要另行审计。

## C-only reset

- 恢复按 GPT 磁盘 GUID、分区 GUID、offset、size 和 64 位 NTFS serial 定位，不使用 Windows 缓存的分区 index；D 保留必须先读 GUID/size/serial/sentinel。
- 读取 NTFS VBR 必须从偏移 0 对齐读取 512 字节再解释字段；不要使用未对齐的 seek 3/72。
- `qemu-img dd` 的 skip/count 语义容易把偏移重复计算；使用脚本已有的 image-opts raw wrapper/有界临时文件，不让小模型重写裸盘逻辑。
- WIM XML 常是 UTF-16 BOM；先按 bytes 解析再交给 XML parser，不能假设 UTF-8。
- 加密/EFS/未知分区必须 fail-closed；不要永久禁用 BitLocker/Device Encryption，只为让恢复脚本通过。
- `--discard` 是明确 destructive 授权：它清 C 上程序、用户文件和工作目录。没有授权、QGA 不通、基线哈希不匹配或 D 身份改变时停止。

## 清理与失败恢复

失败安装的中间文件不需要归档，但删除前必须确认没有 qemu/libvirt、loop、mount、fuser 或 systemd 引用。只清理本实例失败物；保留共享 VirtIO ISO、其他 VM、用户目录和模型服务。不要以“目录看起来大”推断可删对象。

常见证据命令：

```sh
virsh -c qemu:///system domstate NAME
virsh -c qemu:///system dumpxml NAME
findmnt /home/vm_run/vol
fuser -vm /home/vm_run/vol
journalctl -u libvirtd -u NAME-capguard --since '-30 min'
```

清理完成后重新跑对应阶段的只读检查；不要从 seed 直接跳到 finish。若端口能通但 RDP/WinRM 认证、实际桌面、上传哈希或宿主 DROP 证据缺失，验收仍失败。

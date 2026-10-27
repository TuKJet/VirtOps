# 受控入站服务

当 VM 内的 HTTP、MCP 或其他 TCP 服务需要被外部客户端访问时，使用 `network.published_services`。省略该字段时，原有 RDP/WinRM 行为保持兼容。VirtOps 管理端口与隔离，不安装应用或保存其 Token。

“单向访问”指由批准的外部客户端发起连接，VM 可以在该连接中回复数据。TCP 请求、流式 MCP 响应和截图可以共用同一端口；这不授予 VM 主动连接外部模型、内网、DNS 或公网的权限，也不需要给 VM 添加默认网关。

## 声明

例如，将宿主回环 18000 发布到本实例 guest 8000，维护期间禁止访问：

```json
{
  "network": {
    "published_services": [
      {
        "id": "cad-mcp",
        "protocol": "tcp",
        "publish_ip": "127.0.0.1",
        "publish_port": 18000,
        "guest_port": 8000,
        "allowed_sources": ["127.0.0.1/32"],
        "access": "host-loopback",
        "snat_to_bridge": true,
        "allow_during_maintenance": false
      }
    ]
  }
}
```

此片段应合并到实例的完整 profile，不能作为独立 VM profile 使用。新实例经原初始化/创建流程渲染；已有实例使用下面的事务入口。

`host-loopback` 仅发布在 `127.0.0.1`，只生成本地 OUTPUT DNAT，不生成外部接口的 PREROUTING 入口。远端客户端通过宿主 SSH 隧道访问。

`network` 模式须显式指定与实例 `network.publish_ip` 相同的地址，以及 `network.client_cidr` 内的来源子网。不能使用所有接口、所有来源或 guest 子网。它生成来源受限的 PREROUTING DNAT 和 FORWARD 放行，不隐式添加回环入口。

仅支持 TCP；ID、发布端口和 guest 端口在本实例内唯一，额外服务不能复用 RDP/WinRM 的端口。同宿主其他实例也不能占用相同发布端口。`snat_to_bridge` 必须为 true，只针对对应 DNAT 会话定向 SNAT；guest 目标固定来自本实例配置，服务不能指定其他 VM 的 IP。

`allow_during_maintenance` 默认 false。只有显式批准的回环服务可以设为 true；不能把全部业务端口加入维护例外。Windows 防火墙仍须允许该服务的 guest 端口，实际来源为本实例 bridge IP；SNAT 前的客户端来源限制由宿主执行。

## 已有实例的在线变更

在已核对的 Linux 宿主工程目录运行，使用权威 profile。客户端专用的地址覆盖文件不能代替宿主实例配置。将完整的额外服务数组保存到工程外的 `SERVICES.json`，该文件不包含 Token：

常用回环服务不必先写 JSON 文件。按服务 ID 添加，内部端口由应用决定：

```sh
sudo python3 skill/windows-vm-sandbox/scripts/publish_services.py \
  --profile /path/to/instance.json --add-service cad-mcp --guest-port 8000
sudo python3 skill/windows-vm-sandbox/scripts/publish_services.py \
  --profile /path/to/instance.json --add-service cad-mcp --guest-port 8000 --apply
```

省略 `--publish-port` 时，在 18000–18999 中选取未登记、未监听的端口；已存在的同 ID 服务复用原端口。不同 VM 可以复用 guest 8000，但宿主发布端口各自独立。检查结果会展示实际选择；提交时在锁内复查，发生抢占就阻断，不悄悄更换端口。可用 `--publish-port` 指定明确端口；`--remove-service ID` 移除单项服务。HTTP 路径、认证和截图格式不由端口分配器决定。

需要完整来源或维护策略时，使用 JSON 数组入口：

```sh
sudo python3 skill/windows-vm-sandbox/scripts/publish_services.py \
  --profile /path/to/instance.json --services /path/to/SERVICES.json
sudo python3 skill/windows-vm-sandbox/scripts/publish_services.py \
  --profile /path/to/instance.json --services /path/to/SERVICES.json --apply
```

默认仅检查。用户授权增删服务后才 apply；传入的是完整服务列表，空数组表示移除所有额外服务，RDP/WinRM 保留。地址不属于宿主、端口占用、登记冲突、身份不符、未知规则或遗留事务都应阻断，不能猜测替代端口。

事务与 start/stop/watch 共用实例生命周期锁，周期守卫等待变更完成；不关闭 capguard，不主动暂停或重启 VM。维护门禁在 ESTABLISHED 放行之前阻断业务端口的新旧会话，批准的本地管理入口仍可用。每张表原子替换本实例私有链，使用 `iptables-nft-restore --noflush`，不刷新共享父链或其他实例规则。成功后新声明成为守卫的正常校验期望，实例配置、登记和已安装脚本哈希保持一致。

失败在同一把锁内恢复旧文件和本实例规则，并返回受限备份路径。人工恢复前核对状态；使用内置恢复入口及原实例 profile，不手动覆盖共享防火墙：

```sh
sudo python3 skill/windows-vm-sandbox/scripts/publish_services.py \
  --profile /path/to/instance.json --rollback /path/to/reported-backup
```

回退校验失败时保留现场与维护阻断，报告需要恢复的步骤；不能直接 `virsh resume` 或绕过守卫。不要重启宿主或关闭 VM 来测试一次端口变更。

## 验收

- 核对 guest 服务已监听、Windows 防火墙来源限制正确；MCP initialize、tools/list 和预览取回分别验证，端口可连不代表 COM 可用。
- 回环发布经 SSH 隧道可访问，物理/组网地址的同端口不能直接访问；未批准来源继续拒绝。
- 观察多个完整 watch 周期，核对 VM 持续运行且没有新的 guard failure；再次 apply/服务重载后规则无重复。
- 在隔离的 Linux 网络命名空间测试未知规则和规则错序仍被拒绝、维护阻断新旧业务会话，以及 guest 主动 IPv4、宿主访问、DNS/UDP 与 IPv6 的 DROP 计数。破坏性注入不要在用户正在使用的 VM 上做。
- 区分本实例规则与共享 Docker/libvirt/其他 VM 规则；回退和销毁只移除本实例声明。

SolidWorks MCP 的 `streamable-http` 使用 `/mcp`、Bearer Token 和 Host/Origin 校验，预览/文件通过同一 MCP 的产物读取接口取回，参见[应用工程说明](https://github.com/wzyn20051216/solidworks-automation-skill/blob/main/mcp-server/README.md)。认证、桌面会话与截图能力继续由应用工程负责；不要为了这些功能开放额外 DCOM/RPC 或 guest 外联。

# Fixed local Hyper-V provider. $Request is supplied as JSON through stdin.
# No host features are installed and no host restart is initiated by this file.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

function Value($Object, [string]$Name, $Default = $null) {
    if ($null -ne $Object -and $Object.PSObject.Properties.Name -contains $Name) { return $Object.$Name }
    return $Default
}
function Is-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Protect-Directory([string]$Path) {
    $security=[Security.AccessControl.DirectorySecurity]::new()
    $security.SetAccessRuleProtection($true,$false)
    $sids=@([Security.Principal.WindowsIdentity]::GetCurrent().User.Value,'S-1-5-18','S-1-5-32-544')
    foreach($sid in $sids) {
        $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]$sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
    }
    Set-Acl -LiteralPath $Path -AclObject $security
}
function Environment-Info {
    $os = Get-CimInstance Win32_OperatingSystem
    $system = Get-CimInstance Win32_ComputerSystem
    $edition = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').EditionID
    $supported = $edition -match '^(Professional|Enterprise)' -and [int]$os.BuildNumber -ge 19041
    $installed = [bool](Get-Module -ListAvailable Hyper-V)
    [pscustomobject]@{
        platform = 'windows'; provider = 'hyperv-local'; edition = $edition; supported = $supported
        hyperVInstalled = $installed; isAdministrator = (Is-Administrator)
        hypervisorPresent = [bool]$system.HypervisorPresent
        memoryBytes = [long]$system.TotalPhysicalMemory; availableMemoryBytes = [long]$os.FreePhysicalMemory * 1KB
        logicalCpus = [int]$system.NumberOfLogicalProcessors
        ready = $supported -and $installed -and [bool]$system.HypervisorPresent -and (Is-Administrator)
        reason = $(if (-not $supported) { '本机引擎要求 Windows 10/11 专业版或企业版。' }
                   elseif (-not $installed) { '请由管理员启用 Hyper-V 并按 Windows 提示重启；客户端不会自动更改系统功能。' }
                   elseif (-not $system.HypervisorPresent) { 'Hyper-V 未运行，请检查固件虚拟化设置及 Windows 重启状态。' }
                   elseif (-not (Is-Administrator)) { '请以管理员身份运行客户端，以管理本机 Hyper-V 实例。' }
                   else { '' })
    }
}
function Assert-Engine {
    $environment = Environment-Info
    if (-not $environment.ready) { throw $environment.reason }
    Import-Module Hyper-V -ErrorAction Stop
    foreach ($command in @('Get-VMNetworkAdapterExtendedAcl','Add-VMNetworkAdapterExtendedAcl','New-PSSession')) {
        Get-Command $command -ErrorAction Stop | Out-Null
    }
}
function Store-Root {
    $raw = [string](Value $Request 'storeRoot' '')
    if (-not [IO.Path]::IsPathRooted($raw) -or $raw.StartsWith('\\') -or $raw.Contains('..')) { throw 'Invalid local store root' }
    $path = [IO.Path]::GetFullPath($raw).TrimEnd('\')
    if ($path -eq [IO.Path]::GetPathRoot($path).TrimEnd('\')) { throw 'Store cannot be a drive root' }
    if (-not (Test-Path -LiteralPath $path)) { New-Item -ItemType Directory -Path $path | Out-Null }
    if ((Get-Item -LiteralPath $path).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Store cannot be a reparse point' }
    Protect-Directory $path
    return $path
}
function Store-Child([string]$Parent, [string]$Leaf) {
    $target = [IO.Path]::GetFullPath((Join-Path $Parent $Leaf))
    if (-not $target.StartsWith($Parent.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Path escapes local store' }
    if ((Test-Path -LiteralPath $target) -and ((Get-Item -LiteralPath $target).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Managed path is a reparse point' }
    return $target
}
function Save-Json([string]$Path, $Data) {
    $temporary = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [IO.File]::WriteAllText($temporary, ($Data | ConvertTo-Json -Depth 14), [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
}
function Image-Directory([string]$Store) {
    $path = Store-Child $Store 'images'
    if (-not (Test-Path -LiteralPath $path)) { New-Item -ItemType Directory -Path $path | Out-Null }
    return $path
}
function List-Images([string]$Store) {
    $directory = Image-Directory $Store
    return @((Get-ChildItem -LiteralPath $directory -Filter '*.json') | ForEach-Object { Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json })
}
function Mounted-Windows([string]$Disk, [scriptblock]$Body, [switch]$ReadOnly) {
    $mount = $null; $access = $null; $partition = $null
    try {
        $mount = Mount-VHD -Path $Disk -ReadOnly:$ReadOnly -NoDriveLetter -PassThru
        foreach ($candidate in @(Get-Partition -DiskNumber $mount.DiskNumber | Where-Object Size -gt 10GB)) {
            $access = Join-Path ([IO.Path]::GetTempPath()) ('virtops-mount-' + [guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Path $access | Out-Null
            Add-PartitionAccessPath -DiskNumber $mount.DiskNumber -PartitionNumber $candidate.PartitionNumber -AccessPath ($access + '\')
            $partition = $candidate
            if (Test-Path -LiteralPath (Join-Path $access 'Windows\System32\config\SYSTEM')) { return (& $Body $access) }
            Remove-PartitionAccessPath -DiskNumber $mount.DiskNumber -PartitionNumber $candidate.PartitionNumber -AccessPath ($access + '\')
            Remove-Item -LiteralPath $access; $access = $null; $partition = $null
        }
        throw 'Image does not contain a Windows system partition'
    } finally {
        if ($partition -and $access) { Remove-PartitionAccessPath -DiskNumber $mount.DiskNumber -PartitionNumber $partition.PartitionNumber -AccessPath ($access + '\') -ErrorAction SilentlyContinue }
        if ($mount) { Dismount-VHD -Path $Disk -ErrorAction SilentlyContinue }
        if ($access -and (Test-Path -LiteralPath $access)) { Remove-Item -LiteralPath $access -ErrorAction SilentlyContinue }
    }
}
function Import-Image([string]$Store, $Parameters) {
    $source = [string](Value $Parameters 'path' '')
    if (-not [IO.Path]::IsPathRooted($source) -or $source.StartsWith('\\') -or [IO.Path]::GetExtension($source) -ne '.vhdx') { throw 'Select a local, generalized standalone VHDX image' }
    $item = Get-Item -LiteralPath $source
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Image path cannot be a reparse point' }
    $disk = Get-VHD -Path $item.FullName
    if ($disk.Attached -or $disk.ParentPath -or [long]$disk.Size -lt 64GB) { throw 'Image must be detached, standalone, and at least 64 GiB virtual size' }
    Mounted-Windows $item.FullName {
        param($mounted)
        $state = Get-Content -LiteralPath (Join-Path $mounted 'Windows\Setup\State\State.ini') -Raw
        if ($state -notmatch 'IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE') { throw 'Template must be Sysprep generalized and shut down before import' }
    } -ReadOnly
    $hash = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    $directory = Image-Directory $Store
    $id = $hash.Substring(0, 24)
    $destination = Store-Child $directory ($id + '.vhdx')
    $manifest = Store-Child $directory ($id + '.json')
    if (Test-Path -LiteralPath $manifest) { return (Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json) }
    if (Test-Path -LiteralPath $destination) { throw 'Incomplete prior image import exists; inspect it before retrying' }
    Copy-Item -LiteralPath $item.FullName -Destination $destination
    if ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $hash) { throw 'Imported image hash differs' }
    (Get-Item -LiteralPath $destination).IsReadOnly = $true
    $record = [pscustomobject]@{ id = $id; name = [string](Value $Parameters 'name' $item.BaseName); path = $destination; sha256 = $hash; sizeBytes = $item.Length; virtualBytes = [long]$disk.Size }
    Save-Json $manifest $record
    return $record
}
function Load-Record([string]$Store, $Profile) {
    $id = [guid]::Parse([string]$Profile.uuid).ToString()
    $instances = Store-Child $Store 'instances'
    $directory = Store-Child $instances $id
    $record = Get-Content -LiteralPath (Store-Child $directory 'instance.json') -Raw | ConvertFrom-Json
    if ($record.uuid -ne $id -or $record.root -ne $directory -or $record.name -ne $Profile.name) { throw 'Local instance identity differs' }
    return $record
}
function Load-Instance([string]$Store, $Profile) {
    $record=Load-Record $Store $Profile
    $directory=$record.root
    $vm = Get-VM -Id ([guid]$record.vm_id)
    if ($vm.Name -ne $record.vm_name) { throw 'Hyper-V VM identity differs' }
    $drives = @(Get-VMHardDiskDrive -VM $vm)
    if ($drives.Count -ne 1 -or $drives[0].Path -ne (Join-Path $directory 'system.vhdx')) { throw 'Instance disk identity differs' }
    return $record
}
function Expected-Ports($Record) { return @(3389,5986) + @($Record.services | ForEach-Object { [int]$_.guest_port }) }
function Is-Wildcard($Value) { return [string]$Value -in @('', 'Any', '*') }
function Assert-Isolation($Record) {
    $vm = Get-VM -Id ([guid]$Record.vm_id)
    $adapters = @(Get-VMNetworkAdapter -VM $vm)
    if ($adapters.Count -ne 1 -or $adapters[0].SwitchName -ne $Record.switch_name) { throw 'Unexpected VM network adapter or switch' }
    $switch = Get-VMSwitch -Name $Record.switch_name
    if ([string]$switch.SwitchType -ne 'Internal') { throw 'VM switch is not isolated Internal type' }
    $rules = @(Get-VMNetworkAdapterExtendedAcl -VM $vm)
    $ports = @(Expected-Ports $Record)
    if ($rules.Count -ne $ports.Count + 2) { throw 'Unknown or missing Hyper-V isolation rules' }
    foreach ($direction in @('Inbound','Outbound')) {
        $deny = @($rules | Where-Object { [string]$_.Action -eq 'Deny' -and [string]$_.Direction -eq $direction -and $_.Weight -eq 1 -and (Is-Wildcard $_.LocalIPAddress) -and (Is-Wildcard $_.RemoteIPAddress) -and (Is-Wildcard $_.LocalPort) -and (Is-Wildcard $_.RemotePort) -and (Is-Wildcard $_.Protocol) -and -not $_.Stateful })
        if ($deny.Count -ne 1) { throw 'Default isolation deny rule is missing' }
    }
    foreach ($port in $ports) {
        $weight=if($port-eq3389){100}elseif($port-eq5986){101}else{[int](@($Record.services|Where-Object guest_port -eq $port)[0].weight)}
        $allow = @($rules | Where-Object { [string]$_.Action -eq 'Allow' -and [string]$_.Direction -eq 'Inbound' -and [string]$_.LocalIPAddress -eq $Record.guest_ip -and [string]$_.RemoteIPAddress -eq $Record.host_ip -and [string]$_.LocalPort -eq [string]$port -and (Is-Wildcard $_.RemotePort) -and [string]$_.Protocol -in @('TCP','6') -and $_.Stateful -and $_.Weight -eq $weight })
        if ($allow.Count -ne 1) { throw "Declared inbound port $port does not match Hyper-V ACL" }
    }
    return $true
}
function Add-InboundAcl($Record, [int]$Port, [int]$Weight) {
    Add-VMNetworkAdapterExtendedAcl -VMName $Record.vm_name -Direction Inbound -Action Allow -Weight $Weight -LocalIPAddress $Record.guest_ip -RemoteIPAddress $Record.host_ip -LocalPort ([string]$Port) -Protocol TCP -Stateful $true -IdleSessionTimeout 3600
}
function Invoke-Guest($Record, [scriptblock]$Script, [object[]]$Arguments = @()) {
    $credential = Import-Clixml -LiteralPath (Join-Path $Record.root 'credential.clixml')
    $session = $null
    try {
        $session = New-PSSession -VMId ([guid]$Record.vm_id) -Credential $credential
        Invoke-Command -Session $session -ScriptBlock $Script -ArgumentList $Arguments
    } finally { if ($session) { Remove-PSSession $session } }
}
function Stop-OwnedVM($Record) {
    $vm = Get-VM -Id ([guid]$Record.vm_id)
    if ([string]$vm.State -eq 'Off') { return }
    Stop-VM -VM $vm -Confirm:$false
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ([string](Get-VM -Id $vm.Id).State -eq 'Off') { return }
        Start-Sleep -Seconds 1
    }
    throw 'Guest did not shut down; the client will not force power off'
}
function Check-Resources([int]$Cpu, [int]$Memory) {
    $environment = Environment-Info
    if ($Cpu -lt 2 -or $Cpu -gt $environment.logicalCpus -or $Memory -lt 4 -or [long]$Memory * 1GB -gt $environment.memoryBytes - 4GB) { throw 'Resources must fit this host and reserve at least 4 GiB for Windows' }
}
function Free-Subnet {
    $routes = @(Get-NetRoute -AddressFamily IPv4 | Where-Object { $_.DestinationPrefix -ne '0.0.0.0/0' })
    for ($third = 64; $third -lt 250; $third++) {
        $candidate = "172.29.$third.0"
        $bytes = [Net.IPAddress]::Parse($candidate).GetAddressBytes(); [Array]::Reverse($bytes); $number = [BitConverter]::ToUInt32($bytes, 0)
        $conflict = $false
        foreach ($route in $routes) {
            $parts = $route.DestinationPrefix.Split('/'); $length = [int]$parts[1]
            $base = [Net.IPAddress]::Parse($parts[0]).GetAddressBytes(); [Array]::Reverse($base); $prefix = [BitConverter]::ToUInt32($base,0)
            $mask = if ($length -eq 0) { [uint32]0 } else { [uint32]([uint64]4294967295 -shl (32-$length) -band 4294967295) }
            if (($number -band $mask) -eq ($prefix -band $mask)) { $conflict=$true; break }
        }
        if (-not $conflict) { return @("172.29.$third.1", "172.29.$third.2") }
    }
    throw 'No non-conflicting local isolated subnet is available'
}
function Write-Unattend([string]$Disk, [string]$Name, [string]$User, [string]$Password) {
    $safeName = [Security.SecurityElement]::Escape($Name)
    $safeUser = [Security.SecurityElement]::Escape($User)
    $safePassword = [Security.SecurityElement]::Escape($Password)
    $xml = @"
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
 <settings pass="specialize"><component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"><ComputerName>$safeName</ComputerName></component></settings>
 <settings pass="oobeSystem"><component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
  <OOBE><HideEULAPage>true</HideEULAPage><HideOnlineAccountScreens>true</HideOnlineAccountScreens><HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE><ProtectYourPC>3</ProtectYourPC></OOBE>
  <UserAccounts><LocalAccounts><LocalAccount xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" wcm:action="add"><Name>$safeUser</Name><DisplayName>$safeUser</DisplayName><Group>Administrators</Group><Password><Value>$safePassword</Value><PlainText>true</PlainText></Password></LocalAccount></LocalAccounts></UserAccounts>
 </component></settings>
</unattend>
"@
    Mounted-Windows $Disk { param($mounted)
        $target = Join-Path $mounted 'Windows\Panther\Unattend.xml'
        [IO.File]::WriteAllText($target, $xml, [Text.UTF8Encoding]::new($false))
    }
}
function Finish-Guest($Record) {
    Invoke-Guest $Record {
        param($guest,$hostAddress,$name,$administrator)
        $ErrorActionPreference='Stop'
        # Authentication proves specialization has created this instance's user.
        # Remove the one-boot plaintext answer file before network configuration.
        Remove-Item -LiteralPath 'C:\Windows\Panther\Unattend.xml' -Force -ErrorAction SilentlyContinue
        foreach($account in @(Get-LocalUser)) {
            if($account.Name -eq $administrator){continue}
            Disable-LocalUser -SID $account.SID
            if($account.SID.Value -notmatch '-(500|501|503|504)$') {
                $oldSid=$account.SID.Value
                Get-CimInstance Win32_UserProfile|Where-Object {$_.SID -eq $oldSid -and -not $_.Loaded -and -not $_.Special}|Remove-CimInstance
                Remove-LocalUser -SID $account.SID
            }
        }
        $adapter = @(Get-NetAdapter | Where-Object Status -eq 'Up')
        if ($adapter.Count -ne 1) { throw 'Expected exactly one guest network adapter' }
        $index=$adapter[0].ifIndex
        Set-NetIPInterface -InterfaceIndex $index -AddressFamily IPv4 -Dhcp Disabled
        Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object IPAddress -ne $guest | Remove-NetIPAddress -Confirm:$false
        if (-not (Get-NetIPAddress -InterfaceIndex $index -IPAddress $guest -ErrorAction SilentlyContinue)) { New-NetIPAddress -InterfaceIndex $index -IPAddress $guest -PrefixLength 30 | Out-Null }
        Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Remove-NetRoute -Confirm:$false
        Disable-NetAdapterBinding -Name $adapter[0].Name -ComponentID ms_tcpip6 | Out-Null
        Set-NetFirewallProfile -Profile Domain,Private,Public -Enabled True -DefaultInboundAction Block -DefaultOutboundAction Block -LogBlocked True
        foreach($port in @(3389,5986)) {
            $rule='VirtOps-Management-'+$port
            Get-NetFirewallRule -Name $rule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
            New-NetFirewallRule -Name $rule -DisplayName $rule -Direction Inbound -Action Allow -Protocol TCP -LocalAddress $guest -RemoteAddress $hostAddress -LocalPort $port | Out-Null
        }
        Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value 0
        Set-Service TermService -StartupType Automatic; Start-Service TermService
        Enable-PSRemoting -Force -SkipNetworkProfileCheck | Out-Null
        Get-ChildItem WSMan:\localhost\Listener | Where-Object { $_.Keys -contains 'Transport=HTTP' } | Remove-Item -Recurse -Force
        $cert=New-SelfSignedCertificate -DnsName $name -CertStoreLocation Cert:\LocalMachine\My
        Get-ChildItem WSMan:\localhost\Listener|Where-Object {$_.Keys -contains 'Transport=HTTPS'}|Remove-Item -Recurse -Force
        New-Item WSMan:\localhost\Listener -Transport HTTPS -Address '*' -CertificateThumbPrint $cert.Thumbprint -Force|Out-Null
        @{status='ready';guest_ip=$guest;winrm_thumbprint=$cert.Thumbprint}
    } @($Record.guest_ip,$Record.host_ip,$Record.name,$Record.windows.admin_user)
}
function Create-InstanceBody([string]$Store, $Parameters) {
    $name=[string]$Parameters.name; $user=[string]$Parameters.admin_user; $password=[string]$Parameters.password
    if ($name -notmatch '^[a-z][a-z0-9-]{1,11}$' -or $user -notmatch '^[A-Za-z][A-Za-z0-9_.-]{0,19}$' -or $user -match '^(Administrator|Guest|DefaultAccount|WDAGUtilityAccount)$') { throw 'Invalid instance or administrator name' }
    if ($password.Length -lt 8 -or $password.Contains("`r") -or $password.Contains("`n")) { throw 'Use a password of at least 8 characters without line breaks' }
    $cpu=[int]$Parameters.vcpus; $memory=[int]$Parameters.memory_gib
    Check-Resources $cpu $memory
    $imageId=[string]$Parameters.image_id
    if ($imageId -notmatch '^[a-f0-9]{24}$') { throw 'Select an imported base image' }
    $image=Get-Content -LiteralPath (Store-Child (Image-Directory $Store) ($imageId+'.json')) -Raw | ConvertFrom-Json
    if ((Get-FileHash -LiteralPath $image.path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $image.sha256) { throw 'Base image hash differs from its published manifest' }
    $instances=Store-Child $Store 'instances'; New-Item -ItemType Directory -Path $instances -Force|Out-Null
    foreach($file in @(Get-ChildItem -LiteralPath $instances -Filter instance.json -Recurse)) { $existing=Get-Content -LiteralPath $file.FullName -Raw|ConvertFrom-Json; if($existing.name -eq $name){throw 'An instance with this name already exists'} }
    $id=[guid]::NewGuid().ToString();$directory=Store-Child $instances $id;New-Item -ItemType Directory -Path $directory|Out-Null;Protect-Directory $directory
    $switchName='VirtOps-'+$id.Substring(0,8);$vmName='VirtOps-'+$name+'-'+$id.Substring(0,8)
    $addresses=Free-Subnet
    $record=[pscustomobject]@{schema_version=1;provider='hyperv-local';uuid=$id;name=$name;root=$directory;vm_id='';vm_name=$vmName;switch_name=$switchName;switch_id='';host_ip=$addresses[0];guest_ip=$addresses[1];stage='allocating';image_id=$imageId;services=@();created_utc=[DateTime]::UtcNow.ToString('o');resources=@{vcpus=$cpu;memory_gib=$memory};windows=@{admin_user=$user};network=@{guest_ip=$addresses[1];publish_ip=$addresses[1];rdp_port=3389;winrm_port=5986}}
    $recordPath=Join-Path $directory 'instance.json';Save-Json $recordPath $record
    $script:CreatingRecord=$record
    $credential=[pscredential]::new($user,(ConvertTo-SecureString $password -AsPlainText -Force));$credential|Export-Clixml -LiteralPath (Join-Path $directory 'credential.clixml')
    $disk=Join-Path $directory 'system.vhdx'
    Copy-Item -LiteralPath $image.path -Destination $disk; (Get-Item -LiteralPath $disk).IsReadOnly=$false
    $size=[int](Value $Parameters 'disk_gib' ([math]::Ceiling($image.virtualBytes/1GB)))
    if([long]$size*1GB -lt $image.virtualBytes){throw 'Instance disk cannot be smaller than its base image'}
    if([long]$size*1GB -gt $image.virtualBytes){Resize-VHD -Path $disk -SizeBytes ([long]$size*1GB)}
    Write-Unattend $disk $name $user $password
    $switch=New-VMSwitch -Name $switchName -SwitchType Internal
    $record.switch_id=$switch.Id.ToString();$script:CreatedSwitch=$switch
    Save-Json $recordPath $record
    $hostNic=Get-NetAdapter -Name ('vEthernet ('+$switchName+')')
    Set-NetIPInterface -InterfaceIndex $hostNic.ifIndex -AddressFamily IPv4 -Dhcp Disabled -Forwarding Disabled
    New-NetIPAddress -InterfaceIndex $hostNic.ifIndex -IPAddress $record.host_ip -PrefixLength 30|Out-Null
    $vm=New-VM -Name $vmName -Generation 2 -Path $directory -VHDPath $disk -SwitchName $switchName -MemoryStartupBytes ([long]$memory*1GB)
    $script:CreatedVM=$vm
    $record.vm_id=$vm.Id.ToString();$record.stage='defined';Save-Json $recordPath $record
    Set-VMProcessor -VM $vm -Count $cpu
    Set-VM -VM $vm -AutomaticStartAction Nothing -AutomaticStopAction ShutDown
    Set-VMFirmware -VM $vm -EnableSecureBoot On -SecureBootTemplate MicrosoftWindows
    Set-VMKeyProtector -VM $vm -NewLocalKeyProtector;Enable-VMTPM -VM $vm
    Add-VMNetworkAdapterExtendedAcl -VMName $vmName -Action Deny -Direction Inbound -Weight 1
    Add-VMNetworkAdapterExtendedAcl -VMName $vmName -Action Deny -Direction Outbound -Weight 1
    Add-InboundAcl $record 3389 100;Add-InboundAcl $record 5986 101
    Assert-Isolation $record|Out-Null
    $record.stage='booting';Save-Json $recordPath $record;Start-VM -VM $vm|Out-Null
    # Boot/OOBE can exceed a UI request. Never recreate on timeout. A subsequent
    # inspect completes guest configuration using the recorded per-instance ID.
    return @{status='provisioning';profile=$record;note='实例已启动，Windows 正在完成首次配置。稍后检查状态会继续配置网络与管理入口。'}
}
function Create-Instance([string]$Store, $Parameters) {
    $script:CreatingRecord=$null;$script:CreatedSwitch=$null;$script:CreatedVM=$null
    try { return (Create-InstanceBody $Store $Parameters) }
    catch {
        $original=$_.Exception.Message
        if(-not $script:CreatingRecord){throw}
        $record=$script:CreatingRecord
        try {
            if($script:CreatedVM) {
                $vm=Get-VM -Id $script:CreatedVM.Id
                if($vm.Name -ne $record.vm_name){throw 'Created VM identity changed during rollback'}
                if([string]$vm.State -ne 'Off'){Stop-VM -VM $vm -TurnOff -Force -Confirm:$false}
                Remove-VM -VM $vm -Force
            }
            if($script:CreatedSwitch){Remove-VMSwitch -VMSwitch $script:CreatedSwitch -Force}
            $target=Store-Child (Store-Child $Store 'instances') $record.uuid
            if($target -ne $record.root){throw 'Rollback directory differs'}
            Remove-Item -LiteralPath $target -Recurse -Force
        } catch {
            $record.stage='cleanup_required'
            Save-Json (Join-Path $record.root 'instance.json') $record
            return @{status='cleanup_required';profile=$record;reason=$original+'; cleanup: '+$_.Exception.Message}
        }
        throw ($original+'; 本次新建资源已清理，基础镜像未变。')
    }
}
function Delete-Instance([string]$Store, $Profile, $Parameters) {
    $record=Load-Record $Store $Profile
    if([string](Value $Parameters 'confirm_name' '') -cne $record.name){throw 'Confirm the exact VM name to delete all instance data'}
    $record.stage='deleting';Save-Json (Join-Path $record.root 'instance.json') $record
    $vm=$null
    if($record.vm_id){$vm=Get-VM -Id ([guid]$record.vm_id) -ErrorAction SilentlyContinue}
    if(-not $vm){$vm=Get-VM -Name $record.vm_name -ErrorAction SilentlyContinue}
    if($vm) {
        if($record.vm_id -and $vm.Id.ToString() -ne $record.vm_id){throw 'Delete VM identity differs'}
        $disks=@(Get-VMHardDiskDrive -VM $vm)
        if($disks.Count -ne 1 -or $disks[0].Path -ne (Join-Path $record.root 'system.vhdx')){throw 'Delete disk identity differs'}
        if([string]$vm.State -ne 'Off') {
            if((Value $Parameters 'force' $false) -eq $true){Stop-VM -VM $vm -TurnOff -Force -Confirm:$false}
            else {Stop-OwnedVM $record}
        }
        Remove-VM -VM $vm -Force
    }
    $switch=Get-VMSwitch -Name $record.switch_name -ErrorAction SilentlyContinue
    if($switch) {
        if(-not $record.switch_id -or $switch.Id.ToString() -ne $record.switch_id){throw 'Delete switch identity differs'}
        if(@(Get-VMNetworkAdapter -All|Where-Object {$_.SwitchName -eq $record.switch_name -and -not $_.IsManagementOs}).Count){throw 'Another VM uses this switch'}
        Remove-VMSwitch -VMSwitch $switch -Force
    }
    $target=Store-Child (Store-Child $Store 'instances') $record.uuid
    if($target -ne $record.root){throw 'Delete target differs'}
    Remove-Item -LiteralPath $target -Recurse -Force
    return @{status='deleted';uuid=$record.uuid}
}
function Service-GuestRule($Record,$Service,[string]$Operation) {
    Invoke-Guest $Record {
        param($name,$guest,$hostAddress,$port,$operation)
        $ErrorActionPreference='Stop'
        if($operation-eq'ensure') {
            $profiles=@(Get-NetFirewallProfile)
            if($profiles.Count-ne3 -or @($profiles|Where-Object {[string]$_.Enabled-ne'True' -or [string]$_.DefaultOutboundAction-ne'Block'}).Count){throw 'Guest outbound firewall policy differs'}
            if(@(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue).Count){throw 'Guest has an unexpected default route'}
        }
        $rule=@(Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue)
        $created=$false
        if(-not $rule.Count -and $operation-eq'ensure') {
            New-NetFirewallRule -Name $name -DisplayName $name -Enabled True -Direction Inbound -Action Allow -Protocol TCP -LocalAddress $guest -RemoteAddress $hostAddress -LocalPort $port|Out-Null
            $created=$true;$rule=@(Get-NetFirewallRule -Name $name)
        }
        if($rule.Count) {
            $p=$rule|Get-NetFirewallPortFilter;$a=$rule|Get-NetFirewallAddressFilter
            if($rule.Count-ne1 -or [string]$rule[0].Enabled-ne'True' -or [string]$rule[0].Direction-ne'Inbound' -or [string]$rule[0].Action-ne'Allow' -or [string]$p.Protocol-notin@('TCP','6') -or [string]$p.LocalPort-ne[string]$port -or [string]$p.RemotePort-ne'Any' -or [string]$a.LocalAddress-ne$guest -or [string]$a.RemoteAddress-ne$hostAddress){throw 'Managed guest service rule has unexpected scope'}
            if($operation-eq'remove'){$rule|Remove-NetFirewallRule}
        }
        @{created=$created;listener=(@(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue|Where-Object {$_.LocalAddress-in@('0.0.0.0','::',$guest)}).Count-gt0)}
    } @('VirtOps-Service-'+$Service.id,$Record.guest_ip,$Record.host_ip,[int]$Service.guest_port,$Operation)
}
function Remove-ServiceAcl($Record,$Service) {
    $rules=@(Get-VMNetworkAdapterExtendedAcl -VMName $Record.vm_name|Where-Object {$_.Direction-eq'Inbound' -and $_.Weight-eq$Service.weight})
    foreach($rule in $rules) {
        if($rules.Count-ne1 -or [string]$rule.Action-ne'Allow' -or [string]$rule.LocalIPAddress-ne$Record.guest_ip -or [string]$rule.RemoteIPAddress-ne$Record.host_ip -or [string]$rule.LocalPort-ne[string]$Service.guest_port -or -not (Is-Wildcard $rule.RemotePort) -or [string]$rule.Protocol-notin@('TCP','6') -or -not $rule.Stateful){throw 'Pending service ACL identity differs; manual review is required'}
        $rule|Remove-VMNetworkAdapterExtendedAcl -Confirm:$false
    }
}
function Recover-ServiceTransition($Record) {
    $path=Store-Child $Record.root 'service-transition.json'
    if(-not (Test-Path -LiteralPath $path)){return $null}
    $pending=Get-Content -LiteralPath $path -Raw|ConvertFrom-Json
    if($pending.uuid-ne$Record.uuid -or $pending.action-notin@('publish','remove') -or $pending.service.id-notmatch'^[a-z][a-z0-9-]{1,31}$' -or $pending.service.weight-lt200 -or $pending.service.guest_port-lt1 -or $pending.service.guest_port-gt65535 -or $pending.service.guest_ip-ne$Record.guest_ip){throw 'Service recovery identity differs'}
    # Close the exact ingress before restoring metadata or touching the guest.
    Remove-ServiceAcl $Record $pending.service
    $Record.services=@(if($pending.action-eq'publish'){$pending.before}else{$pending.before|Where-Object id -ne $pending.service.id})
    Save-Json (Join-Path $Record.root 'instance.json') $Record
    Assert-Isolation $Record|Out-Null
    try {
        if($pending.cleanup_guest){Service-GuestRule $Record $pending.service 'remove'|Out-Null}
    } catch {
        return @{status='cleanup_required';host_closed=$true;service=$pending.service;reason=$_.Exception.Message}
    }
    Remove-Item -LiteralPath $path -Force
    return $null
}
function Publish-LocalService($Record,$Parameters) {
    Assert-Isolation $Record|Out-Null
    $port=[int](Value $Parameters 'guest_port' 0);$id=[string](Value $Parameters 'service_id' '')
    if($port-lt1 -or $port-gt65535 -or $port-in@(3389,5986)){throw 'Choose an additional TCP port between 1 and 65535'}
    if(-not $id){$id='tcp-'+$port}
    if($id-notmatch'^[a-z][a-z0-9-]{1,31}$'){throw 'Invalid service ID'}
    $existing=@($Record.services|Where-Object id -eq $id)
    if($existing.Count -and $existing[0].guest_port-ne$port){throw 'Service ID belongs to another guest port'}
    if(@($Record.services|Where-Object {$_.guest_port-eq$port -and $_.id-ne$id}).Count){throw 'Guest port is already declared'}
    if($existing.Count){$service=$existing[0];$guest=Service-GuestRule $Record $service 'ensure'}
    else {
        $weight=200;while(@($Record.services|Where-Object weight -eq $weight).Count){$weight++}
        if($weight-gt65000){throw 'Too many published services'}
        $service=[pscustomobject]@{id=$id;guest_port=$port;guest_ip=$Record.guest_ip;weight=$weight}
        $path=Store-Child $Record.root 'service-transition.json'
        # Persist intent before mutation. Recovery revokes only this new port.
        $pending=[pscustomobject]@{uuid=$Record.uuid;action='publish';service=$service;before=@($Record.services);cleanup_guest=$true}
        Save-Json $path $pending
        try {
            $guest=Service-GuestRule $Record $service 'ensure'
            $pending.cleanup_guest=[bool]$guest.created;Save-Json $path $pending
            $Record.services=@($Record.services)+@($service)
            Save-Json (Join-Path $Record.root 'instance.json') $Record
            Add-InboundAcl $Record $port $weight
            Assert-Isolation $Record|Out-Null
            Remove-Item -LiteralPath $path -Force
        } catch {
            $reason=$_.Exception.Message
            $recovery=Recover-ServiceTransition $Record
            if($recovery){return $recovery}
            throw $reason
        }
    }
    $connected=$false;$client=[Net.Sockets.TcpClient]::new()
    try {$connect=$client.ConnectAsync($Record.guest_ip,$port);if($connect.Wait(3000)){$connected=$client.Connected}}catch{}finally{$client.Dispose()}
    return @{status=$(if($connected){'ready'}else{'configured'});service=$service;local_target=($Record.guest_ip+':'+$port);guest_listener=[bool]$guest.listener;tcp_reachable=$connected;note='TCP 连通性不代表 HTTP/MCP 身份认证成功。'}
}
function Remove-LocalService($Record,$Parameters) {
    Assert-Isolation $Record|Out-Null
    $id=[string](Value $Parameters 'service_id' '')
    if($id-notmatch'^[a-z][a-z0-9-]{1,31}$'){throw 'Invalid service ID'}
    $existing=@($Record.services|Where-Object id -eq $id)
    if(-not $existing.Count){return @{status='absent';service_id=$id}}
    $service=$existing[0]
    Save-Json (Store-Child $Record.root 'service-transition.json') ([pscustomobject]@{uuid=$Record.uuid;action='remove';service=$service;before=@($Record.services);cleanup_guest=$true})
    $recovery=Recover-ServiceTransition $Record
    if($recovery){return $recovery}
    return @{status='removed';service=$service;host_closed=$true}
}
function Instance-Action([string]$Store, [string]$Action, $Profile, $Parameters) {
    if($Action -eq 'delete'){return (Delete-Instance $Store $Profile $Parameters)}
    $record=Load-Instance $Store $Profile; $vm=Get-VM -Id ([guid]$record.vm_id);$recordPath=Join-Path $record.root 'instance.json'
    $recovery=Recover-ServiceTransition $record
    if($recovery -and $Action-notin@('inspect','status','start','stop','logs')){return $recovery}
    if($Action -eq 'inspect' -or $Action -eq 'status') {
        Assert-Isolation $record|Out-Null
        $warning=$null
        if($record.stage -eq 'booting' -and [string]$vm.State -eq 'Running') {
            try { Finish-Guest $record|Out-Null;$record.stage='ready';Save-Json $recordPath $record }
            catch {
                if(([DateTime]::UtcNow-[DateTime]$record.created_utc).TotalMinutes -gt 30){$record.stage='bootstrap_failed';Save-Json $recordPath $record;$warning='首次配置超过 30 分钟。检查模板的 OOBE 与账户策略，或确认删除该实例后重新创建。'+$_.Exception.Message}
                else {$warning='Windows 首次配置尚未完成，稍后重试。'+$_.Exception.Message}
            }
        }
        return @{status='inspected';domain=@{state=([string]$vm.State).ToLowerInvariant();vcpus=$vm.ProcessorCount;memory_KiB=([long]$vm.MemoryStartup/1KB)};profile=$record;stage=$record.stage;warning=$warning}
    }
    if($Action -eq 'start') {Assert-Isolation $record|Out-Null;Start-VM -VM $vm|Out-Null;return @{status='started'}}
    if($Action -eq 'stop') {Stop-OwnedVM $record;return @{status='stopped'}}
    if($Action -eq 'configure') {
        $cpu=[int]$Parameters.vcpus;$memory=[int]$Parameters.memory_gib;Check-Resources $cpu $memory
        $wasRunning=[string]$vm.State -ne 'Off'
        if($wasRunning -and -not (Value $Parameters 'stop' $false)){throw 'Changing resources requires explicit shutdown authorization'}
        if($wasRunning){Stop-OwnedVM $record}
        Set-VMProcessor -VM $vm -Count $cpu;Set-VMMemory -VM $vm -DynamicMemoryEnabled $false -StartupBytes ([long]$memory*1GB)
        $record.resources.vcpus=$cpu;$record.resources.memory_gib=$memory;Save-Json $recordPath $record
        if($wasRunning -and (Value $Parameters 'restart' $false)){Assert-Isolation $record|Out-Null;Start-VM -VM $vm|Out-Null}
        return @{status='applied';profile=$record}
    }
    if($Action -eq 'services'){Assert-Isolation $record|Out-Null;return @{status='ok';services=@($record.services)}}
    if($Action -eq 'publish'){return (Publish-LocalService $record $Parameters)}
    if($Action -eq 'remove_service'){return (Remove-LocalService $record $Parameters)}
    if($Action -eq 'list_files') {
        $path=[string](Value $Parameters 'path' 'C:\')
        if($path -notmatch '^[A-Za-z]:\\' -or $path.Contains('..') -or $path -match '[\r\n]'){throw 'Use an absolute guest drive path'}
        $items=@(Invoke-Guest $record {param($p);Get-ChildItem -LiteralPath $p -Force|Select-Object -First 500 Name,FullName,@{n='directory';e={$_.PSIsContainer}},@{n='bytes';e={if($_.PSIsContainer){0}else{$_.Length}}}} @($path))
        return @{status='ok';path=$path;items=$items;limit=500}
    }
    if($Action -eq 'logs') {
        $samples=@(Invoke-Guest $record {Get-Content -LiteralPath 'C:\Windows\System32\LogFiles\Firewall\pfirewall.log' -Tail 250 -ErrorAction SilentlyContinue|Where-Object {$_ -notmatch '^#' -and $_ -match ' DROP '}})
        return @{status='ok';network_samples=$samples;note='Windows 防火墙拦截样本；Hyper-V ACL 丢弃不一定出现在 guest 日志中。'}
    }
    throw 'Unsupported local VM operation'
}

try {
    $action=[string]$Request.action
    if($action -eq 'environment') {$result=Environment-Info}
    else {
        Assert-Engine;$store=Store-Root;$parameters=Value $Request 'parameters' ([pscustomobject]@{})
        $mutex=[Threading.Mutex]::new($false,'Global\VirtOps-HyperV-Local')
        $locked=$false
        try {
            $locked=$mutex.WaitOne(0)
            if(-not $locked){throw 'Another local Hyper-V operation is running'}
            switch($action) {
                'list_images' {$result=@{status='ok';images=@(List-Images $store)}}
                'import_image' {$result=@{status='imported';image=(Import-Image $store $parameters)}}
                'inspect_iso' {$result=Inspect-Iso $store $parameters}
                'build_image' {$result=Build-BaseImage $store $parameters}
                'create' {$result=Create-Instance $store $parameters}
                default {$result=Instance-Action $store $action $Request.profile $parameters}
            }
        } finally {if($locked){$mutex.ReleaseMutex()};$mutex.Dispose()}
    }
    $result|ConvertTo-Json -Depth 16 -Compress
} catch {
    @{status='blocked';reason=$_.Exception.Message}|ConvertTo-Json -Compress
    exit 2
}

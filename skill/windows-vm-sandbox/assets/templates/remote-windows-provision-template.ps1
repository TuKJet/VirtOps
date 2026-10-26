#Requires -RunAsAdministrator
<#
Template only. A separate seed ISO (not a modified Microsoft ISO) must contain
at its root: VMSEED and this file named remote-windows-provision.ps1. The
unattend specialize pass copies it to C:\Windows\Setup\Scripts before OOBE.

The template installs only from the attached virtio-win 0.1.302 ISO. It neither
downloads content nor logs credentials. The VM definition must expose the QEMU
guest-agent virtio-serial channel named org.qemu.guest_agent.0.
#>
[CmdletBinding()]
param(
    [string]$AdminUser = '__BOOTSTRAP_ADMIN_USER__',
    [string]$GuestAddress = '172.30.61.2',
    [int]$PrefixLength = 30,
    [string[]]$AllowedRemoteAddresses = @('172.30.61.1'),
    [string]$CertificateDnsName = 'winvm01'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$logPath = 'C:\Windows\Temp\winvm01-provision.log'

function Write-ErrorSummary([System.Exception]$Exception) {
    $safeMessage = $Exception.Message -replace '(?i)(password|secret|token)\s*[:=]\s*\S+', '$1=[redacted]'
    Add-Content -LiteralPath $logPath -Value ("{0:o} ERROR {1}" -f (Get-Date), $safeMessage)
}

function Invoke-NativeChecked([string]$FilePath, [string[]]$ArgumentList) {
    & $FilePath @ArgumentList | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "$FilePath failed with exit code $LASTEXITCODE." }
}

function Clear-BootstrapAutoLogon {
    $winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    Remove-ItemProperty -Path $winlogon -Name DefaultPassword -ErrorAction SilentlyContinue
    Set-ItemProperty -Path $winlogon -Name AutoAdminLogon -Value '0'
}

function Get-VirtioMediaRoot {
    $volume = Get-Volume | Where-Object {
        $_.DriveType -eq 'CD-ROM' -and $_.FileSystemLabel -match '^virtio-win'
    } | Select-Object -First 1
    if (-not $volume -or -not $volume.DriveLetter) {
        throw 'The virtio-win ISO is not attached or has no usable volume label.'
    }
    return ('{0}:\' -f $volume.DriveLetter)
}

function Install-MsiQuietly([string]$Path) {
    $result = Start-Process -FilePath "$env:SystemRoot\System32\msiexec.exe" -ArgumentList @('/i', ('"{0}"' -f $Path), '/qn', '/norestart') -Wait -PassThru
    if ($result.ExitCode -notin 0, 3010) {
        throw "MSI installation failed with exit code $($result.ExitCode)."
    }
}

function Get-GuestAdapter {
    for ($attempt = 1; $attempt -le 12; $attempt++) {
        $candidates = @(Get-NetAdapter -IncludeHidden:$false | Where-Object {
            $_.InterfaceDescription -notmatch 'Loopback' -and $_.Status -ne 'Disabled'
        })
        if ($candidates.Count -eq 1) {
            $candidate = $candidates[0]
            Enable-NetAdapter -Name $candidate.Name -Confirm:$false -ErrorAction SilentlyContinue
            if ((Get-NetAdapter -Name $candidate.Name).Status -eq 'Up') { return $candidate }
        }
        Start-Sleep -Seconds 2
    }
    throw 'Expected exactly one active guest NIC after NetKVM driver installation.'
}

trap {
    Write-ErrorSummary -Exception $_.Exception
    exit 1
}

# Do these first: a later failure must neither leave the answer-file password in
# Winlogon nor retain a hibernation file sized for the 128 GiB guest.
Clear-BootstrapAutoLogon
Invoke-NativeChecked -FilePath "$env:SystemRoot\System32\powercfg.exe" -ArgumentList @('/hibernate', 'off')
$crashControl = 'HKLM:\SYSTEM\CurrentControlSet\Control\CrashControl'
Set-ItemProperty -Path $crashControl -Name CrashDumpEnabled -Type DWord -Value 0
Set-ItemProperty -Path $crashControl -Name AlwaysKeepMemoryDump -Type DWord -Value 0

$virtioRoot = Get-VirtioMediaRoot

# SATA carries the system disk. Install NetKVM and virtio-serial explicitly;
# the latter is required before QEMU Guest Agent can use its host channel.
$netInf = Join-Path $virtioRoot 'NetKVM\w11\amd64\netkvm.inf'
$vioserInf = Join-Path $virtioRoot 'vioserial\w11\amd64\vioser.inf'
foreach ($inf in @($netInf, $vioserInf)) {
    if (-not (Test-Path -LiteralPath $inf)) { throw "Required VirtIO driver not found: $inf" }
    & "$env:SystemRoot\System32\pnputil.exe" /add-driver $inf /install | Out-Null
    $driverExit = $LASTEXITCODE
    # 259 also covers an already better/newer driver; it is not evidence of
    # success by itself. NIC state and a live QGA connection are verified later.
    if ($driverExit -notin 0, 259, 3010) { throw "PnP driver install failed: exit=$driverExit inf=$inf" }
    Add-Content -LiteralPath $logPath -Value ("{0:o} DRIVER exit={1} inf={2}" -f (Get-Date), $driverExit, $inf)
}

$qgaMsi = Join-Path $virtioRoot 'guest-agent\qemu-ga-x86_64.msi'
if (-not (Test-Path -LiteralPath $qgaMsi)) { throw 'QEMU Guest Agent x64 MSI was not found on the virtio-win ISO.' }
Install-MsiQuietly -Path $qgaMsi
$qga = Get-Service -Name 'QEMU-GA' -ErrorAction Stop
Set-Service -Name $qga.Name -StartupType Automatic
Start-Service -Name $qga.Name -ErrorAction SilentlyContinue

$nic = Get-GuestAdapter
Disable-NetAdapterBinding -Name $nic.Name -ComponentID ms_tcpip6 -ErrorAction Stop
Get-NetIPAddress -InterfaceIndex $nic.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '127.*' } |
    Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
Set-NetIPInterface -InterfaceIndex $nic.ifIndex -AddressFamily IPv4 -Dhcp Disabled
New-NetIPAddress -InterfaceIndex $nic.ifIndex -IPAddress $GuestAddress -PrefixLength $PrefixLength | Out-Null
# This is the isolated point-to-point bridge, with default-deny firewall rules
# for every profile. WinRM endpoint registration rejects a Public profile.
Get-NetConnectionProfile -InterfaceIndex $nic.ifIndex -ErrorAction Stop |
    Set-NetConnectionProfile -NetworkCategory Private -ErrorAction Stop

# No default gateway and no DNS: the guest cannot originate a routed connection.
$interfaceRegistry = "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces\$($nic.InterfaceGuid)"
Set-ItemProperty -Path $interfaceRegistry -Name NameServer -Value ''
Set-ItemProperty -Path $interfaceRegistry -Name DhcpNameServer -Value ''

# Default-deny only applies to new outbound traffic; replies for explicitly
# admitted inbound sessions are statefully permitted.
Set-NetFirewallProfile -Profile Domain,Private,Public -DefaultInboundAction Block -DefaultOutboundAction Block
Get-NetFirewallRule -DisplayName 'Remote VM baseline *' -ErrorAction SilentlyContinue | Remove-NetFirewallRule

# HTTPS WinRM only. The management controller should pin this certificate;
# TrustedHosts is deliberately not configured.
Set-Service -Name WinRM -StartupType Automatic
Start-Service -Name WinRM
Invoke-NativeChecked -FilePath "$env:SystemRoot\System32\winrm.cmd" -ArgumentList @('set', 'winrm/config/service', '@{AllowUnencrypted="false"}')
Invoke-NativeChecked -FilePath "$env:SystemRoot\System32\winrm.cmd" -ArgumentList @('set', 'winrm/config/service/auth', '@{Basic="false"}')
Get-ChildItem -Path WSMan:\localhost\Listener | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
$san = '2.5.29.17={text}dns=winvm01&ipaddress=172.30.61.2&ipaddress=192.168.20.5&ipaddress=127.0.0.1'
$cert = New-SelfSignedCertificate -Subject "CN=$CertificateDnsName" -TextExtension $san -CertStoreLocation 'Cert:\LocalMachine\My' -KeyAlgorithm RSA -KeyLength 3072 -HashAlgorithm SHA256
New-WSManInstance -ResourceURI 'winrm/config/Listener' -SelectorSet @{ Address = '*'; Transport = 'HTTPS' } -ValueSet @{ CertificateThumbprint = $cert.Thumbprint } | Out-Null

# RDP uses the same vmadmin account as AI management. There is no UDP
# 3389 firewall rule in this profile.
Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value 0
Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp' -Name UserAuthentication -Value 1
Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp' -Name SecurityLayer -Value 2
# AI has administrator rights inside the guest. The host firewall and bounded
# storage provide the sandbox boundary; do not grant this account host access.
$administratorsGroup = Get-LocalGroup -SID 'S-1-5-32-544'
if (-not (Get-LocalGroupMember $administratorsGroup | Where-Object { $_.SID -eq (Get-LocalUser $AdminUser).SID })) {
    Add-LocalGroupMember -Group $administratorsGroup -Member $AdminUser
}
New-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -Name LocalAccountTokenFilterPolicy -PropertyType DWord -Value 1 -Force | Out-Null
Enable-PSSessionConfiguration -Name Microsoft.PowerShell -Force | Out-Null
Set-Service -Name WinRM -StartupType Automatic
Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Services\WinRM' -Name DelayedAutoStart -Value 0
Start-Service -Name WinRM
Get-ChildItem -Path WSMan:\localhost\Listener | Where-Object { $_.Keys -contains 'Transport=HTTP' } |
    Remove-Item -Recurse -Force -ErrorAction Stop

$agentWorkspace = 'C:\Work'
New-Item -ItemType Directory -Path $agentWorkspace -Force | Out-Null
$workspaceAcl = New-Object System.Security.AccessControl.DirectorySecurity
$workspaceAcl.SetAccessRuleProtection($true, $false)
foreach ($sid in @('S-1-5-18', 'S-1-5-32-544')) {
    $rights = 'FullControl'
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        (New-Object System.Security.Principal.SecurityIdentifier($sid)), $rights,
        'ContainerInherit,ObjectInherit', 'None', 'Allow')
    [void]$workspaceAcl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $agentWorkspace -AclObject $workspaceAcl

# Disable host-backed paths through RDP resource redirection.
$tsPolicy = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services'
New-Item -Path $tsPolicy -Force | Out-Null
New-ItemProperty -Path $tsPolicy -Name fDisableCdm -PropertyType DWord -Value 1 -Force | Out-Null
New-ItemProperty -Path $tsPolicy -Name fDisableClip -PropertyType DWord -Value 1 -Force | Out-Null
New-ItemProperty -Path $tsPolicy -Name fDisableCpm -PropertyType DWord -Value 1 -Force | Out-Null

New-NetFirewallRule -DisplayName 'Remote VM baseline WinRM HTTPS' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5986 -RemoteAddress $AllowedRemoteAddresses | Out-Null
New-NetFirewallRule -DisplayName 'Remote VM baseline RDP TCP' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 3389 -RemoteAddress $AllowedRemoteAddresses | Out-Null
Set-Service -Name TermService -StartupType Automatic
Start-Service -Name TermService

# Windows OOBE can leave the well-known "defaultuser0" placeholder account and
# its profile behind. A template must hold exactly one provisioned account and
# no foreign profile, so the placeholder is removed before the template
# preflight inspects this builder.
$placeholder = Get-LocalUser -Name 'defaultuser0' -ErrorAction SilentlyContinue
if ($placeholder) {
    Get-CimInstance -ClassName Win32_UserProfile | Where-Object { $_.SID -eq $placeholder.SID.Value } | Remove-CimInstance -ErrorAction SilentlyContinue
    Remove-LocalUser -SID $placeholder.SID -ErrorAction SilentlyContinue
}

# Windows 11 enables Device Encryption on its own when a TPM is present, which
# leaves a half-encrypted volume that cannot be captured as a template. The
# policy travels with the published image, so derived instances stay
# unencrypted as well.
$deviceEncryption = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DeviceEncryption'
New-Item -Path $deviceEncryption -Force | Out-Null
New-ItemProperty -Path $deviceEncryption -Name 'AllowDeviceEncryption' -PropertyType DWord -Value 0 -Force | Out-Null
New-ItemProperty -Path $deviceEncryption -Name 'DisableDeviceEncryption' -PropertyType DWord -Value 1 -Force | Out-Null
$volume = Get-BitLockerVolume -MountPoint C: -ErrorAction SilentlyContinue
if ($volume -and [string]$volume.VolumeStatus -ne 'FullyDecrypted') {
    manage-bde.exe -off C: | Out-Null
    $deadline = (Get-Date).AddMinutes(10)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 15
        $volume = Get-BitLockerVolume -MountPoint C: -ErrorAction SilentlyContinue
        if (-not $volume -or [string]$volume.VolumeStatus -eq 'FullyDecrypted') { break }
    }
}

# Keep a bounded page file and record a credential-free success marker for
# host-side QGA validation after the required reboot.
$computerSystem = Get-CimInstance -ClassName Win32_ComputerSystem
Set-CimInstance -InputObject $computerSystem -Property @{ AutomaticManagedPagefile = $false } | Out-Null
Get-CimInstance -ClassName Win32_PageFileSetting -ErrorAction SilentlyContinue | Remove-CimInstance -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management' -Name PagingFiles -Type MultiString -Value @('C:\pagefile.sys 4096 8192')
$timeSettings = 'HKLM:\SYSTEM\CurrentControlSet\Control\TimeZoneInformation'
New-ItemProperty -Path $timeSettings -Name RealTimeIsUniversal -PropertyType DWord -Value 1 -Force | Out-Null
Set-TimeZone -Id 'China Standard Time'
Set-Content -LiteralPath 'C:\Windows\Temp\winvm01-provision.success' -Value ((Get-Date).ToUniversalTime().ToString('o')) -NoNewline
Restart-Computer -Force

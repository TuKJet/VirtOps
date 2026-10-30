[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidateSet('inspect','ensure','remove')] [string] $Action,
    [Parameter(Mandatory)] [string] $ProfilePath,
    [Parameter(Mandatory)] [string] $ServiceId,
    [Parameter(Mandatory)] [ValidateRange(1,65535)] [int] $GuestPort,
    [string] $GuestIp,
    [string] $BridgeIp
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Fail([string] $Message) { throw "service_guest.ps1: $Message" }
function Read-ServiceProfile {
    if (-not (Test-Path -LiteralPath $ProfilePath -PathType Leaf)) { Fail "profile not found: $ProfilePath" }
    try { $profile = Get-Content -LiteralPath $ProfilePath -Raw | ConvertFrom-Json } catch { Fail "invalid profile JSON: $($_.Exception.Message)" }
    foreach ($path in @('uuid','network.guest_ip','network.bridge_ip','network.publish_ip','network.winrm_port','windows.computer_name','windows.admin_user','winrm_thumbprint','credentials.local_clixml')) {
        $value = $profile; foreach ($part in $path.Split('.')) { $value = $value.$part }
        if ([string]::IsNullOrWhiteSpace([string]$value)) { Fail "profile field is required: $path" }
    }
    if ([string]::IsNullOrWhiteSpace($GuestIp)) { $GuestIp = [string]$profile.network.guest_ip }
    if ([string]::IsNullOrWhiteSpace($BridgeIp)) { $BridgeIp = [string]$profile.network.bridge_ip }
    if ($GuestIp -ne [string]$profile.network.guest_ip) { Fail 'GuestIp must match profile.network.guest_ip' }
    if ($BridgeIp -ne [string]$profile.network.bridge_ip) { Fail 'BridgeIp must match profile.network.bridge_ip' }
    if ($ServiceId -notmatch '^[a-z][a-z0-9-]{1,31}$') { Fail 'ServiceId must be a lowercase id' }
    if ([string]$profile.uuid -notmatch '^[0-9a-fA-F-]{36}$') { Fail 'profile.uuid is invalid' }
    foreach ($address in @($GuestIp,$BridgeIp)) {
        $parsed = $null
        if (-not [Net.IPAddress]::TryParse($address, [ref]$parsed) -or $parsed.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { Fail 'GuestIp and BridgeIp must be IPv4 addresses' }
    }
    return $profile
}
function Invoke-GuestOperation($Session, [scriptblock] $Script, [object[]] $Arguments = @()) {
    return Invoke-Command -Session $Session -ScriptBlock $Script -ArgumentList $Arguments -ErrorAction Stop
}
# Reuse the existing pinned WinRM/certificate and identity path without running guest.ps1's main action.
$callerAction = $Action
. (Join-Path $PSScriptRoot 'guest.ps1') -Action inspect -ProfilePath $ProfilePath -LibraryOnly
$Action = $callerAction
$session = $null
try {
    $profile = Read-ServiceProfile
    if ([string]::IsNullOrWhiteSpace($GuestIp)) { $GuestIp = [string]$profile.network.guest_ip }
    if ([string]::IsNullOrWhiteSpace($BridgeIp)) { $BridgeIp = [string]$profile.network.bridge_ip }
    $ruleName = 'VirtOps-' + ([string]$profile.uuid) + '-' + $ServiceId
    $session = New-WinRmSession $profile
    $result = Invoke-GuestOperation $session {
        param($mode,$name,$guest,$bridge,$port)
        function Get-RemoteIsolation {
            $profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop)
            $requiredProfiles = @('Domain','Private','Public')
            if ($profiles.Count -ne 3 -or @($requiredProfiles | Where-Object { $name = $_; -not @($profiles | Where-Object { $_.Name -eq $name }) }).Count -gt 0 -or @($profiles | Where-Object { -not $_.Enabled -or [string]$_.DefaultOutboundAction -ne 'Block' }).Count -gt 0) { throw 'guest firewall Domain, Private, and Public profiles must all be enabled with DefaultOutboundAction=Block' }
            $routes = @(Get-NetRoute -ErrorAction Stop)
            if (@($routes | Where-Object { $_.DestinationPrefix -in @('0.0.0.0/0','::/0') }).Count -gt 0) { throw 'guest default route exists; refusing to change inbound firewall' }
            $profileState = @($profiles | ForEach-Object { [pscustomobject]@{ Name = [string]$_.Name; Enabled = [bool]$_.Enabled; DefaultOutboundAction = [string]$_.DefaultOutboundAction } })
            [pscustomobject]@{ firewall_profiles = $profileState; default_ipv4_routes = 0; default_ipv6_routes = 0 }
        }
        function Get-RemoteRule([string] $ruleName, [string] $expectedGuestIp, [string] $expectedBridgeIp, [int] $expectedPort) {
            $rule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
            if (-not $rule) { return $null }
            $portFilter = $rule | Get-NetFirewallPortFilter
            $addressFilter = $rule | Get-NetFirewallAddressFilter
            $local = @([string[]]$addressFilter.LocalAddress)
            $remote = @([string[]]$addressFilter.RemoteAddress)
            $matches = ([string]$rule.Direction -eq 'Inbound' -and [string]$rule.Action -eq 'Allow' -and [string]$rule.Enabled -eq 'True' -and
                [string]$portFilter.Protocol -eq 'TCP' -and [string]$portFilter.LocalPort -eq [string]$expectedPort -and
                ($local -contains $expectedGuestIp) -and ($remote -contains $expectedBridgeIp) -and $local.Count -eq 1 -and $remote.Count -eq 1)
            [pscustomobject]@{ matches = [bool]$matches; local_address = $local; remote_address = $remote; local_port = [string]$portFilter.LocalPort; protocol = [string]$portFilter.Protocol }
        }
        $iso = Get-RemoteIsolation
        $state = Get-RemoteRule $name $guest $bridge $port
        $created = $false
        if ($state -and -not $state.matches) { throw 'existing owned firewall rule does not match the requested exact scope' }
        if ($mode -eq 'ensure') {
            if (-not $state) {
                New-NetFirewallRule -Name $name -DisplayName $name -Direction Inbound -Action Allow -Enabled True -Profile Any -Protocol TCP -LocalAddress $guest -RemoteAddress $bridge -LocalPort $port -ErrorAction Stop | Out-Null
                $created = $true
                try { $state = Get-RemoteRule $name $guest $bridge $port; if (-not $state -or -not $state.matches) { throw 'firewall rule readback failed' } }
                catch { Remove-NetFirewallRule -Name $name -ErrorAction SilentlyContinue; throw }
            }
        } elseif ($mode -eq 'remove') {
            if ($state) { Remove-NetFirewallRule -Name $name -ErrorAction Stop; $state = $null }
        }
        $listener = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Where-Object { $_.LocalAddress -in @($guest,'0.0.0.0') }).Count -gt 0
        [pscustomobject]@{ status = 'ok'; action = $mode; rule_name = $name; created = $created; present = [bool]$state; listener = $listener; ready = [bool]$state -and $listener; isolation = $iso }
    } @($Action,$ruleName,$GuestIp,$BridgeIp,$GuestPort)
    $result | ConvertTo-Json -Depth 8
} catch {
    [pscustomobject]@{ status = 'blocked'; action = $Action; reason = $_.Exception.Message } | ConvertTo-Json -Depth 6
    exit 2
} finally { if ($session) { Remove-PSSession $session -ErrorAction SilentlyContinue } }

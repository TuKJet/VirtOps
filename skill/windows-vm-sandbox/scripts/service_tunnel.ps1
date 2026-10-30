[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidateSet('start','status','stop')] [string] $Action,
    [Parameter(Mandatory)] [string] $ProfilePath,
    [Parameter(Mandatory)] [ValidatePattern('^[a-z][a-z0-9-]{1,31}$')] [string] $ServiceId,
    [Parameter(Mandatory)] [ValidateRange(1,65535)] [int] $PublishPort,
    [ValidateRange(0,65535)] [int] $LocalPort = 0,
    [string] $StateDir,
    [switch] $LibraryOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Fail([string] $Message) { throw "service_tunnel.ps1: $Message" }
function Emit($Value) { $Value | ConvertTo-Json -Depth 8 -Compress }

function Assert-Token([string] $Value, [string] $Name, [string] $Pattern) {
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -notmatch $Pattern -or $Value.Contains("`r") -or $Value.Contains("`n")) {
        Fail "$Name contains unsafe characters"
    }
}

function Read-Profile {
    if (-not (Test-Path -LiteralPath $ProfilePath -PathType Leaf)) { Fail "profile not found" }
    try { $profile = Get-Content -LiteralPath $ProfilePath -Raw | ConvertFrom-Json } catch { Fail "invalid profile JSON" }
    foreach ($field in @('name','uuid','ssh.host','ssh.user','ssh.port','network.publish_port')) {
        # network.publish_port is intentionally not required; the CLI argument
        # is the authoritative declared host port for this tunnel.
        if ($field -eq 'network.publish_port') { continue }
        $value = $profile
        foreach ($part in $field.Split('.')) { $value = $value.$part }
        if ($null -eq $value -or [string]::IsNullOrWhiteSpace([string]$value)) { Fail "profile field is required: $field" }
    }
    Assert-Token ([string]$profile.name) 'profile.name' '^[a-z][a-z0-9-]{1,31}$'
    Assert-Token ([string]$profile.uuid) 'profile.uuid' '^[0-9a-fA-F-]{8,64}$'
    Assert-Token ([string]$profile.ssh.host) 'ssh.host' '^[A-Za-z0-9][A-Za-z0-9.:-]{0,253}$'
    Assert-Token ([string]$profile.ssh.user) 'ssh.user' '^[A-Za-z_][A-Za-z0-9_.-]{0,63}$'
    if ([int]$profile.ssh.port -lt 1 -or [int]$profile.ssh.port -gt 65535) { Fail 'ssh.port is invalid' }
    if ($profile.ssh.PSObject.Properties.Name -contains 'identity_file' -and $profile.ssh.identity_file) {
        $identity = [Environment]::ExpandEnvironmentVariables([string]$profile.ssh.identity_file)
        if ([IO.Path]::IsPathRooted($identity) -eq $false -or $identity.Contains('"') -or $identity.Contains("`r") -or $identity.Contains("`n") -or $identity.StartsWith('-')) { Fail 'ssh.identity_file must be an absolute path without quote/newline characters' }
    }
    if ($profile.ssh.PSObject.Properties.Name -contains 'bind_address' -and $profile.ssh.bind_address) {
        $ip = $null
        if (-not [Net.IPAddress]::TryParse([string]$profile.ssh.bind_address, [ref]$ip) -or $ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { Fail 'ssh.bind_address must be IPv4' }
    }
    if ($profile.ssh.PSObject.Properties.Name -contains 'host_key_alias' -and $profile.ssh.host_key_alias) { Assert-Token ([string]$profile.ssh.host_key_alias) 'ssh.host_key_alias' '^[A-Za-z0-9][A-Za-z0-9._:-]{0,253}$' }
    return $profile
}

function Get-StateDirectory($Profile, [bool]$Create = $true) {
    $target = if ($StateDir) { $StateDir } elseif ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'VirtOps\tunnels' } else { Join-Path $env:TEMP 'VirtOps\tunnels' }
    $target = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($target))
    if ($target.Contains("`r") -or $target.Contains("`n")) { Fail 'StateDir contains a newline' }
    if (-not (Test-Path -LiteralPath $target)) {
        if (-not $Create) { return $target }
        New-Item -ItemType Directory -Path $target -Force | Out-Null
    }
    if (-not (Get-Item -LiteralPath $target).PSIsContainer) { Fail 'StateDir is not a directory' }
    return $target
}

function Get-RecordPath($Directory, $Profile) {
    return Join-Path $Directory (([string]$Profile.uuid).ToLowerInvariant() + '-' + $ServiceId + '.json')
}

function Get-MutexName($Profile) {
    $bytes = [Text.Encoding]::UTF8.GetBytes(([string]$Profile.uuid).ToLowerInvariant() + ':' + $ServiceId)
    $hash = [Security.Cryptography.SHA256]::Create().ComputeHash($bytes)
    return 'Global\VirtOpsTunnel-' + ([BitConverter]::ToString($hash) -replace '-', '').Substring(0,24)
}

function Invoke-WithMutex($Profile, [scriptblock] $Body) {
    $mutex = [Threading.Mutex]::new($false, (Get-MutexName $Profile))
    try {
        if (-not $mutex.WaitOne([TimeSpan]::FromSeconds(15))) { Fail 'tunnel operation is busy' }
        & $Body
    } finally {
        try { $mutex.ReleaseMutex() } catch {}
        $mutex.Dispose()
    }
}

function Read-Record($Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try { return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json } catch { Fail 'tunnel state is invalid' }
}

function Assert-RecordBinding($Record, $Profile) {
    if ($null -eq $Record) { return }
    if ([string]$Record.uuid -ne [string]$Profile.uuid -or [string]$Record.name -ne [string]$Profile.name -or
        [string]$Record.service_id -ne $ServiceId -or [int]$Record.publish_port -ne $PublishPort -or
        [string]$Record.remote_host -ne '127.0.0.1') { Fail 'tunnel state does not match profile/service; refusing operation' }
    if ($null -eq $Record.local_port -or [int]$Record.local_port -lt 1024 -or [int]$Record.local_port -gt 65535) { Fail 'tunnel state has an invalid local port; refusing operation' }
    if ($null -eq $Record.arguments -or $null -eq $Record.command_line -or $null -eq $Record.process_start_utc) { Fail 'tunnel state is incomplete; refusing operation' }
}

function Write-Record($Path, $Value) {
    $temporary = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporary -Encoding UTF8
        $null = Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
}

function Get-SshPath { return (Get-Command ssh.exe -ErrorAction Stop).Source }

function Get-Arguments($Profile, [int] $Port) {
    $ssh = $Profile.ssh
    $remote = [string]$ssh.user + '@' + [string]$ssh.host
    $args = @('-N','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ExitOnForwardFailure=yes',
        '-o','ServerAliveInterval=30','-o','ServerAliveCountMax=2','-o','ConnectTimeout=10',
        '-L',("127.0.0.1:{0}:127.0.0.1:{1}" -f $Port,$PublishPort), '-p',[string]$ssh.port)
    if ($ssh.PSObject.Properties.Name -contains 'bind_address' -and $ssh.bind_address) { $args += @('-o',('BindAddress=' + [string]$ssh.bind_address)) }
    if ($ssh.PSObject.Properties.Name -contains 'identity_file' -and $ssh.identity_file) { $args += @('-i',[Environment]::ExpandEnvironmentVariables([string]$ssh.identity_file)) }
    if ($ssh.PSObject.Properties.Name -contains 'host_key_alias' -and $ssh.host_key_alias) { $args += @('-o',('HostKeyAlias=' + [string]$ssh.host_key_alias)) }
    $args += $remote
    return $args
}

function Quote-WindowsArgument([string]$Value) {
    if ($Value -notmatch '[\s"]') { return $Value }
    if ($Value.Contains("`r") -or $Value.Contains("`n")) { Fail 'SSH argument contains a newline' }
    return '"' + (($Value -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
}

function Get-CanonicalCommandLine([string]$Executable, [object[]]$Arguments) {
    return ((Quote-WindowsArgument $Executable) + ' ' + (($Arguments | ForEach-Object { Quote-WindowsArgument ([string]$_) }) -join ' ')).Trim()
}

function Get-CommandLineVariants([string]$Executable, [object[]]$Arguments) {
    $tail = (($Arguments | ForEach-Object { Quote-WindowsArgument ([string]$_) }) -join ' ')
    return @((Get-CanonicalCommandLine $Executable $Arguments), ('"' + $Executable + '" ' + $tail).Trim()) | Select-Object -Unique
}

function Get-FreeLoopbackPort {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    try { $listener.Start(); return ([Net.IPEndPoint]$listener.LocalEndpoint).Port } finally { $listener.Stop() }
}

function Start-DetachedProcess([string]$Path, [object[]]$Arguments) {
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $Path
    $info.UseShellExecute = $true
    $info.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $info.CreateNoWindow = $true
    $argumentListProperty = $info.GetType().GetProperty('ArgumentList')
    if ($null -ne $argumentListProperty) {
        foreach ($argument in $Arguments) { [void]$info.ArgumentList.Add([string]$argument) }
    } else {
        $info.Arguments = (($Arguments | ForEach-Object { Quote-WindowsArgument ([string]$_) }) -join ' ')
    }
    $process = [Diagnostics.Process]::Start($info)
    if ($null -eq $process) { Fail 'failed to start detached SSH process' }
    return $process
}

function Test-OwnedProcess($Record, [string]$SshPath, [object[]]$ExpectedArguments) {
    if ($null -eq $Record -or $null -eq $Record.pid) { return $null }
    try { $process = Get-Process -Id ([int]$Record.pid) -ErrorAction Stop } catch { return $null }
    try {
        if (@($Record.arguments).Count -ne @($ExpectedArguments).Count) { return $null }
        for ($index = 0; $index -lt @($ExpectedArguments).Count; $index++) {
            if ([string]$Record.arguments[$index] -cne [string]$ExpectedArguments[$index]) { return $null }
        }
        $started = $process.StartTime.ToUniversalTime().ToString('o')
        $recordStarted = ([DateTime]$Record.process_start_utc).ToUniversalTime().ToString('o')
        if ([string]$Record.executable -ne $SshPath -or $recordStarted -ne $started) { return $null }
        $cmd = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f [int]$Record.pid) -ErrorAction Stop
        if ([string]$cmd.ExecutablePath -ne $SshPath) { return $null }
        $expectedLines = @(Get-CommandLineVariants $SshPath $ExpectedArguments)
        if ($expectedLines -notcontains [string]$cmd.CommandLine -or $expectedLines -notcontains [string]$Record.command_line) { return $null }
        return $process
    } catch { return $null }
}

function Test-LocalPort([int]$Port) {
    try {
        $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port); $listener.Start(); $listener.Stop(); return $false
    } catch { return $true }
}

function Get-ListenerOwner([int]$Port) {
    try {
        $connection = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1
        if ($connection) { return [int]$connection.OwningProcess }
    } catch {}
    return $null
}

function Wait-Ready($Process, [int]$Port) {
    $deadline = [DateTime]::UtcNow.AddSeconds(12)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($Process.HasExited) { Fail "ssh exited before tunnel became ready (code $($Process.ExitCode))" }
        if ((Get-ListenerOwner $Port) -eq $Process.Id) { return $true }
        Start-Sleep -Milliseconds 200
    }
    Fail 'SSH tunnel did not open its local port'
}

function Start-Tunnel($Profile, $RecordPath) {
    $sshPath = Get-SshPath
    $record = Read-Record $RecordPath
    Assert-RecordBinding $record $Profile
    if ($record -and $LocalPort -and [int]$record.local_port -ne $LocalPort) { Fail 'LocalPort differs from the existing tunnel; stop it before changing the port' }
    $requested = if ($LocalPort) { $LocalPort } elseif ($record) { [int]$record.local_port } else { $PublishPort }
    if ($requested -lt 1024 -and $requested -ne 0) { Fail 'LocalPort must be 1024..65535' }
    $arguments = Get-Arguments $Profile $requested
    $owned = Test-OwnedProcess $record $sshPath $arguments
    if ($owned) { return [pscustomobject]@{status='ok'; action='start'; created=$false; reused=$true; local_port=$requested; pid=$owned.Id; record=$RecordPath} }
    if ($record -and (Test-LocalPort $requested)) {
        Fail 'recorded LocalPort is already occupied by an unowned process'
    } elseif (-not $record -and (Test-LocalPort $requested)) {
        if ($LocalPort) { Fail 'requested LocalPort is already occupied by another process' }
        $requested = Get-FreeLoopbackPort; $arguments = Get-Arguments $Profile $requested
    }
    $process = Start-DetachedProcess $sshPath $arguments
    $recordWritten = $false
    try {
        $cmd = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $process.Id) -ErrorAction Stop
        $expectedLines = @(Get-CommandLineVariants $sshPath $arguments)
        if ([string]$cmd.ExecutablePath -ne $sshPath -or $expectedLines -notcontains [string]$cmd.CommandLine) {
            Fail 'started SSH process identity does not match requested tunnel'
        }
        $record = [ordered]@{schema_version=1; name=[string]$Profile.name; uuid=[string]$Profile.uuid; service_id=$ServiceId;
            local_port=$requested; publish_port=$PublishPort; remote_host='127.0.0.1'; pid=$process.Id;
            executable=$sshPath; arguments=@($arguments); process_start_utc=($process.StartTime.ToUniversalTime().ToString('o'));
            command_line=[string]$cmd.CommandLine}
        Write-Record $RecordPath $record
        $recordWritten = $true
        $null = Wait-Ready $process $requested
        return [pscustomobject]@{status='ok'; action='start'; created=$true; reused=$false; local_port=$requested; pid=$process.Id; record=$RecordPath}
    } catch {
        if ($recordWritten) {
            try {
                Stop-Tunnel $Profile $RecordPath | Out-Null
            } catch {
                throw "SSH tunnel startup failed and cleanup failed; tunnel state was retained: $($_.Exception.Message)"
            }
        } elseif (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            $exited = $process.WaitForExit(5000)
            if (-not $exited -or -not $process.HasExited) { throw 'SSH tunnel startup failed and process cleanup could not be confirmed' }
        }
        throw
    }
}

function Get-Status($Profile, $RecordPath) {
    $record = Read-Record $RecordPath
    if (-not $record) { return [pscustomobject]@{status='ok'; action='status'; running=$false; record=$RecordPath} }
    Assert-RecordBinding $record $Profile
    $sshPath = Get-SshPath
    $existing = $null
    try { $existing = Get-Process -Id ([int]$record.pid) -ErrorAction Stop } catch {}
    $arguments = Get-Arguments $Profile ([int]$record.local_port)
    $process = Test-OwnedProcess $record $sshPath $arguments
    if ($existing -and -not $process) { Fail 'recorded process identity does not match; refusing operation' }
    return [pscustomobject]@{status='ok'; action='status'; running=[bool]$process; pid=if($process){$process.Id}else{$null}; local_port=[int]$record.local_port; record=$RecordPath}
}

function Stop-Tunnel($Profile, $RecordPath) {
    $record = Read-Record $RecordPath
    if (-not $record) { return [pscustomobject]@{status='ok'; action='stop'; stopped=$false; record=$RecordPath} }
    Assert-RecordBinding $record $Profile
    $existing = $null
    try { $existing = Get-Process -Id ([int]$record.pid) -ErrorAction Stop } catch {}
    $process = Test-OwnedProcess $record (Get-SshPath) (Get-Arguments $Profile ([int]$record.local_port))
    if ($existing -and -not $process) { Fail 'recorded process identity does not match; refusing to stop it' }
    if ($process) {
        Stop-Process -Id $process.Id -Force
        $exited = $process.WaitForExit(5000)
        if (-not $exited -or -not $process.HasExited) {
            Fail 'owned SSH process did not exit after stop; tunnel state was retained'
        }
    }
    Remove-Item -LiteralPath $RecordPath -Force
    return [pscustomobject]@{status='ok'; action='stop'; stopped=[bool]$process; record=$RecordPath}
}

if ($LibraryOnly) { return }

try {
    $profile = Read-Profile
    if ($PublishPort -lt 1 -or $PublishPort -gt 65535) { Fail 'PublishPort is invalid' }
    $directory = Get-StateDirectory $profile ($Action -ne 'status')
    $recordPath = Get-RecordPath $directory $profile
    $result = Invoke-WithMutex $profile {
        switch ($Action) {
            'start' { Start-Tunnel $profile $recordPath }
            'status' { Get-Status $profile $recordPath }
            'stop' { Stop-Tunnel $profile $recordPath }
        }
    }
    Emit $result
    exit 0
} catch {
    Emit ([pscustomobject]@{status='blocked'; action=$Action; reason=$_.Exception.Message})
    exit 2
}

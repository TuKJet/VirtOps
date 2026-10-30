[CmdletBinding()]
param()
$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest
$project=Split-Path -Parent $PSScriptRoot
$tool=Join-Path $project 'skill/windows-vm-sandbox/scripts/service_tunnel.ps1'
$errors=$null; $tokens=$null
[Management.Automation.Language.Parser]::ParseFile($tool,[ref]$tokens,[ref]$errors)|Out-Null
if($errors.Count){throw 'service_tunnel.ps1 parse failed'}
$source=Get-Content -LiteralPath $tool -Raw
foreach($required in @('BatchMode=yes','StrictHostKeyChecking=yes','ExitOnForwardFailure=yes','ServerAliveInterval=30','-L','127.0.0.1:')){
    if($source -notlike "*$required*"){throw "missing SSH safety invariant: $required"}
}
if($source -match "'-R'|'-g'|'-o','GatewayPorts"){throw 'unsafe reverse/global forwarding option present'}

$profilePath=Join-Path $project ('.validation/tunnel-profile-'+[guid]::NewGuid().ToString('N')+'.json')
$state=Join-Path $project ('.validation/tunnel-state-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path (Split-Path $profilePath) -Force|Out-Null
$profile=[ordered]@{name='winvm01';uuid='8cb9f0f6-3d77-4c1c-8d0b-5ab70ac5f201';ssh=[ordered]@{host='192.0.2.5';user='ops';port=22;bind_address='192.0.2.10';identity_file='C:\keys\vm';host_key_alias='vm-host'};network=[ordered]@{publish_port=18000}}
$profile|ConvertTo-Json -Depth 8|Set-Content -LiteralPath $profilePath -Encoding UTF8
try {
    . $tool -Action status -ProfilePath $profilePath -ServiceId cad-mcp -PublishPort 18000 -StateDir $state -LibraryOnly
    $pwshPath=(Get-Command pwsh.exe -ErrorAction Stop).Source
    $timer=[Diagnostics.Stopwatch]::StartNew()
    $detached=Start-DetachedProcess $pwshPath @('-NoProfile','-Command','Start-Sleep -Seconds 2')
    $timer.Stop()
    if($timer.Elapsed.TotalSeconds -ge 1 -or $detached.HasExited){throw 'detached child launch blocked or inherited caller pipes'}
    $detached.WaitForExit(5000)|Out-Null
    $args=Get-Arguments ($profile|ConvertTo-Json|ConvertFrom-Json) 18001
    $joined=$args -join ' '
    if($joined -notmatch '127\.0\.0\.1:18001:127\.0\.0\.1:18000'){throw 'forward tuple is wrong'}
    if($joined -match ' -R | -g '){throw 'reverse/global forwarding leaked'}
    if($args -notcontains 'StrictHostKeyChecking=yes'){throw 'strict host key checking missing'}
    $name=Get-MutexName ($profile|ConvertTo-Json|ConvertFrom-Json)
    if($name -notmatch '^Global\\VirtOpsTunnel-[0-9A-F]{24}$'){throw 'mutex name is not deterministic'}
    $record=[pscustomobject]@{pid=$PID;executable='wrong';process_start_utc='wrong';command_line='wrong'}
    if(Test-OwnedProcess $record (Get-Command powershell.exe).Source @()){throw 'mismatched process accepted'}

    $fake=[pscustomobject]@{Id=4242;HasExited=$false;ExitCode=0;StartTime=[DateTime]::Parse('2026-10-30T01:02:03Z').ToLocalTime()}
    $fake | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param([int]$Milliseconds); return $true }
    function global:Get-SshPath { 'C:\Windows\ssh.exe' }
    function global:Test-LocalPort([int]$Port) { $false }
    function global:Get-ListenerOwner([int]$Port) { 4242 }
    function global:Get-Process { param([int]$Id,[object]$ErrorAction); if($Id -eq 4242){$fake}else{throw 'missing process'} }
    function global:Get-CimInstance { param([string]$ClassName,[string]$Filter,[object]$ErrorAction); [pscustomobject]@{ExecutablePath='C:\Windows\ssh.exe';CommandLine=(Get-CanonicalCommandLine 'C:\Windows\ssh.exe' (Get-Arguments ($profile|ConvertTo-Json|ConvertFrom-Json) 18000))} }
    function global:Start-DetachedProcess { param([string]$Path,[object[]]$Arguments); $fake }
    $stopped=$false
    $forceStopFails=$false
    function global:Stop-Process([int]$Id, [switch]$Force) { $script:stopped=$true; if(-not $script:forceStopFails){$fake.HasExited=$true} }
    New-Item -ItemType Directory -Path $state -Force|Out-Null
    $created=Start-Tunnel ($profile|ConvertTo-Json|ConvertFrom-Json) (Join-Path $state 'record.json')
    if(-not $created.created -or $created.reused){throw 'mocked start did not create a tunnel'}
    $rr=Get-Content (Join-Path $state 'record.json') -Raw | ConvertFrom-Json
    $reused=Start-Tunnel ($profile|ConvertTo-Json|ConvertFrom-Json) (Join-Path $state 'record.json')
    if(-not $reused.reused -or $reused.pid -ne 4242){throw 'mismatched current argv was reused or reuse failed'}
    $changedProfile=$profile | ConvertTo-Json -Depth 8 | ConvertFrom-Json
    $changedProfile.ssh.host='192.0.2.6'
    $mismatchRefused=$false
    try { Get-Status $changedProfile (Join-Path $state 'record.json') | Out-Null } catch { $mismatchRefused=$true }
    if(-not $mismatchRefused){throw 'changed SSH destination was accepted as the owned tunnel'}
    $stoppedResult=Stop-Tunnel ($profile|ConvertTo-Json|ConvertFrom-Json) (Join-Path $state 'record.json')
    if(-not $stoppedResult.stopped -or -not $stopped){throw 'owned process was not stopped'}
    $fake.HasExited=$false; $forceStopFails=$true
    function global:Wait-Ready { throw 'simulated readiness failure' }
    $readyFailed=$false
    try { Start-Tunnel ($profile|ConvertTo-Json|ConvertFrom-Json) (Join-Path $state 'record.json') | Out-Null } catch { $readyFailed=$true }
    if(-not $readyFailed -or -not (Test-Path -LiteralPath (Join-Path $state 'record.json') -PathType Leaf)){throw 'readiness failure did not retain tunnel state when cleanup failed'}
    Remove-Item -LiteralPath (Join-Path $state 'record.json') -Force
    $forceStopFails=$false
    Remove-Item Function:\Test-LocalPort -Force
    $occupied=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,19001); $occupied.Start()
    try {
        $LocalPort=19001
        $occupiedRefused=$false
        try { Start-Tunnel ($profile|ConvertTo-Json|ConvertFrom-Json) (Join-Path $state 'record.json') | Out-Null } catch { $occupiedRefused=$true }
        if(-not $occupiedRefused){throw 'occupied requested local port was accepted'}
    } finally { $occupied.Stop() }
    Write-Output 'service tunnel tests: PASS (parse, forwarding scope, mutex, occupied port, process identity)'
} finally {
    Remove-Item -LiteralPath $profilePath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $state -Recurse -Force -ErrorAction SilentlyContinue
}

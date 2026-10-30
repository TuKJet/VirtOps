$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$path=Join-Path (Split-Path -Parent $PSScriptRoot) 'desktop/engine/local_hyperv.ps1'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseInput([IO.File]::ReadAllText($path,[Text.Encoding]::UTF8),[ref]$tokens,[ref]$errors)
if($errors.Count){throw ($errors|Out-String)}
# Load function definitions only. No main dispatch or host operation is run.
foreach($definition in $ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]},$false)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
function Must-Reject([scriptblock]$Body,[string]$Reason) {
    $rejected=$false;try{& $Body|Out-Null}catch{$rejected=$true;$script:lastRejection=$_.Exception.Message}
    if(-not $rejected){throw "Expected rejection: $Reason"}
}
$record=[pscustomobject]@{vm_id=[guid]::NewGuid().ToString();vm_name='owned';switch_name='owned-switch';guest_ip='172.29.64.2';host_ip='172.29.64.1';services=@()}
function Get-VM {param($Id) [pscustomobject]@{Id=$Id;Name='owned'}}
function Get-VMNetworkAdapter {param($VM) [pscustomobject]@{SwitchName='owned-switch'}}
function Get-VMSwitch {param($Name) [pscustomobject]@{SwitchType='Internal'}}
function Get-VMNetworkAdapterExtendedAcl {param($VM,$VMName) $script:rules}
function New-Rule($Direction,$Action,$Weight,$Port='*') {
    [pscustomobject]@{Direction=$Direction;Action=$Action;Weight=$Weight;LocalIPAddress=$(if($Action-eq'Allow'){$record.guest_ip}else{'*'});RemoteIPAddress=$(if($Action-eq'Allow'){$record.host_ip}else{'*'});LocalPort=[string]$Port;RemotePort='*';Protocol=$(if($Action-eq'Allow'){'TCP'}else{'Any'});Stateful=($Action-eq'Allow')}
}
$script:rules=@((New-Rule Inbound Deny 1),(New-Rule Outbound Deny 1),(New-Rule Inbound Allow 100 3389),(New-Rule Inbound Allow 101 5986))
Assert-Isolation $record|Out-Null
$script:rules[1].RemoteIPAddress='1.1.1.1'
Must-Reject {Assert-Isolation $record} 'narrow deny is not default deny'
$script:rules[1].RemoteIPAddress='*'
$script:rules[2].Weight=999
Must-Reject {Assert-Isolation $record} 'undeclared ACL priority'
$script:rules[2].Weight=100
$script:rules+=New-Rule Outbound Allow 200 443
Must-Reject {Assert-Isolation $record} 'unknown outbound allow'
Must-Reject {Store-Child 'C:\managed' '..\outside'} 'path traversal'
$temp=Join-Path ([IO.Path]::GetTempPath()) ('virtops-contract-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temp|Out-Null
try {
    $record|Add-Member uuid ([guid]::NewGuid().ToString())
    $record|Add-Member root $temp
    $script:rules=$script:rules[0..3]
    $script:guestRemoved=$false;$script:failGuestRemoval=$false
    function Service-GuestRule($Record,$Service,$Operation) {
        if($Operation-eq'remove'){
            if($script:failGuestRemoval){throw 'mock guest temporarily offline'}
            $script:guestRemoved=$true
        }
        return @{created=$true;listener=$false}
    }
    function Add-InboundAcl {throw 'mock host ACL write failure'}
    Must-Reject {Publish-LocalService $record ([pscustomobject]@{guest_port=8000})} 'failed publication'
    if($record.services.Count-ne0 -or -not $script:guestRemoved -or (Test-Path -LiteralPath (Join-Path $temp 'service-transition.json'))){throw ('Publication recovery failure: '+$script:lastRejection)}
    $service=[pscustomobject]@{id='tcp-8000';guest_ip=$record.guest_ip;guest_port=8000;weight=200}
    $record.services=@($service)
    $script:rules+=New-Rule Inbound Allow 200 8000
    function Remove-VMNetworkAdapterExtendedAcl {param([Parameter(ValueFromPipeline=$true)]$Rule,[switch]$Confirm) process{$script:rules=@($script:rules|Where-Object Weight -ne $Rule.Weight)}}
    $script:failGuestRemoval=$true
    $outcome=Remove-LocalService $record ([pscustomobject]@{service_id=$service.id})
    if($outcome.status-ne'cleanup_required' -or -not $outcome.host_closed -or $record.services.Count-ne0 -or @($script:rules|Where-Object Weight -eq 200).Count){throw 'Partial removal failed to close host ingress first'}
    $script:failGuestRemoval=$false
    Recover-ServiceTransition $record|Out-Null
    if(Test-Path -LiteralPath (Join-Path $temp 'service-transition.json')){throw 'Removal recovery did not clear its journal'}
} finally {
    $resolved=[IO.Path]::GetFullPath($temp)
    if(-not $resolved.StartsWith([IO.Path]::GetTempPath(),[StringComparison]::OrdinalIgnoreCase) -or (Split-Path $resolved -Leaf)-notlike'virtops-contract-*'){throw 'Invalid test cleanup path'}
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
Write-Output 'local Hyper-V contract tests: PASS'

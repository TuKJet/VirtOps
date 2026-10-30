[CmdletBinding()]
param()
$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest
$tool = Join-Path (Split-Path -Parent $PSScriptRoot) 'skill/windows-vm-sandbox/scripts/service_guest.ps1'
$guest = Join-Path (Split-Path -Parent $PSScriptRoot) 'skill/windows-vm-sandbox/scripts/guest.ps1'
$errors=$null; $ast=[Management.Automation.Language.Parser]::ParseFile($tool,[ref]$null,[ref]$errors)
if($errors.Count){throw 'service guest tool parse failed'}
$guestAst=[Management.Automation.Language.Parser]::ParseFile($guest,[ref]$null,[ref]$errors)
if($errors.Count){throw 'guest tool parse failed'}
$source=[IO.File]::ReadAllText($tool)
if($source -notmatch 'DefaultOutboundAction.*Block' -or $source -notmatch 'New-NetFirewallRule' -or $source -notmatch 'Remove-NetFirewallRule'){throw 'missing isolation or scoped rule operations'}
if($source -match 'Set-NetFirewallProfile|New-NetRoute|Remove-NetRoute'){throw 'tool must not alter outbound policy or routes'}
$names=@('Read-ServiceProfile','Invoke-GuestOperation')
foreach($name in $names){if(-not $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)){throw "missing function $name"}}
$guestParam=$guestAst.FindAll({param($n) $n -is [Management.Automation.Language.ParameterAst] -and $n.Name.VariablePath.UserPath -eq 'LibraryOnly'},$true)
if($guestParam.Count -ne 1){throw 'guest.ps1 must expose LibraryOnly for safe reuse'}
'Mocked remote operation tests:'
$remoteBlocks=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.ScriptBlockExpressionAst] -and $n.ScriptBlock.ParamBlock -and ($n.ScriptBlock.ParamBlock.Parameters.Name.VariablePath.UserPath -join ',') -eq 'mode,name,guest,bridge,port'},$true))
if($remoteBlocks.Count -ne 1){throw 'remote service operation block not found'}
$operation=$remoteBlocks[0].ScriptBlock.GetScriptBlock()
$script:rules=@{}
$script:routes=@()
function Get-NetFirewallProfile { param($PolicyStore) @([pscustomobject]@{Name='Domain';Enabled=$true;DefaultOutboundAction='Block'},[pscustomobject]@{Name='Private';Enabled=$true;DefaultOutboundAction='Block'},[pscustomobject]@{Name='Public';Enabled=$true;DefaultOutboundAction='Block'}) }
function Get-NetRoute { param($DestinationPrefix) $script:routes }
function Get-NetFirewallRule { param($Name) $script:rules[$Name] }
function Get-NetFirewallPortFilter { param([Parameter(ValueFromPipeline)]$InputObject) process { [pscustomobject]@{Protocol=$InputObject.Protocol;LocalPort=$InputObject.LocalPort} } }
function Get-NetFirewallAddressFilter { param([Parameter(ValueFromPipeline)]$InputObject) process { [pscustomobject]@{LocalAddress=$InputObject.LocalAddress;RemoteAddress=$InputObject.RemoteAddress} } }
function New-NetFirewallRule { param($Name,$DisplayName,$Direction,$Action,$Enabled,$Profile,$Protocol,$LocalAddress,$RemoteAddress,$LocalPort) $script:rules[$Name]=[pscustomobject]@{Name=$Name;Direction=$Direction;Action=$Action;Enabled=$Enabled;Protocol=$Protocol;LocalAddress=$LocalAddress;RemoteAddress=$RemoteAddress;LocalPort=$LocalPort}; $script:rules[$Name] }
function Remove-NetFirewallRule { param($Name) $script:rules.Remove($Name) | Out-Null }
function Get-NetTCPConnection { param($State,$LocalPort) @([pscustomobject]@{LocalAddress='172.30.61.2';LocalPort=$LocalPort;State='Listen'}) }
$args=@('ensure','VirtOps-uuid-mcp','172.30.61.2','172.30.61.1',8000)
$first=& $operation @args
if(-not $first.created -or -not $first.present -or -not $first.listener -or -not $first.ready -or $script:rules.Count -ne 1){throw 'ensure did not create an isolated exact rule'}
if($first.isolation.firewall_profiles[0].Enabled -isnot [bool] -or $first.isolation.firewall_profiles[0].DefaultOutboundAction -isnot [string]){throw 'isolation result must contain JSON primitives'}
$second=& $operation @args
if($second.created -or $script:rules.Count -ne 1){throw 'ensure was not idempotent'}
$script:rules['VirtOps-uuid-mcp'].Action='Block'
$failed=$false; try { & $operation @args | Out-Null } catch {$failed=$true}
if(-not $failed -or $script:rules['VirtOps-uuid-mcp'].Action -ne 'Block'){throw 'mismatched owned rule was not rejected'}
$script:rules['VirtOps-uuid-mcp'].Action='Allow'
$removed=& $operation 'remove' 'VirtOps-uuid-mcp' '172.30.61.2' '172.30.61.1' 8000
if($removed.present -or $script:rules.Count -ne 0){throw 'remove did not delete only the owned rule'}
$script:rules=@{}; $script:routes=@([pscustomobject]@{DestinationPrefix='0.0.0.0/0'})
$failed=$false; try { & $operation @args | Out-Null } catch {$failed=$true}
if(-not $failed -or $script:rules.Count -ne 0){throw 'default route did not fail closed before writes'}
if($source -notmatch '\$callerAction\s*=\s*\$Action' -or $source -notmatch '\$Action\s*=\s*\$callerAction'){throw 'caller action was not preserved across guest library loading'}
'service guest tests: PASS (parse, isolation gate, exact rule operations, no route/outbound mutation)'

<#
.SYNOPSIS
Preview or validate an explicit candidate Python without activating it.
.DESCRIPTION
AuditOnly checks metadata with no model imports. RunGpuSmoke is a separate,
explicit idle-3080 test; it never starts TouchDesigner, a worker listener, or a
camera. A pass is not live/thermal acceptance and never changes a launcher.
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('moge2', 'depth_anything')][string]$Provider,
    [Parameter(Mandatory = $true)][string]$WorkerPython,
    [Parameter(Mandatory = $true)]
    [ValidateSet('3080ti_16gb', '4090', '5090')][string]$Profile,
    [ValidateRange(0, 31)][int]$GpuIndex = 0,
    [string]$NvidiaSmi = '',
    [string]$Model = '',
    [string]$Report = '',
    [switch]$AuditOnly,
    [switch]$RunGpuSmoke
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
. (Join-Path $PSScriptRoot '_WorkerPython.Common.ps1')
. (Join-Path $PSScriptRoot '_LocalVerification.Common.ps1')
if ($AuditOnly -and $RunGpuSmoke) { throw 'Choose AuditOnly or RunGpuSmoke, not both.' }
if ($RunGpuSmoke -and $Profile -ne '3080ti_16gb') {
    throw 'This synthetic geometry smoke is 3080-specific; it cannot certify 4090/5090 profiles.'
}
$python = Resolve-FlexGpuWorkerOverride -Root $root -WorkerPython $WorkerPython
if (-not $Report) { $Report = "runtime/validation/worker-$Profile-$Provider-latest.json" }
$reportPath = Resolve-FlexGpuLocalReportPath -Root $root -Path $Report
$mode = if ($RunGpuSmoke) { 'gpu_smoke' } elseif ($AuditOnly) { 'audit_only' } else { 'preview' }
if ($mode -eq 'preview') {
    [ordered]@{ status='preview'; mode=$mode; provider=$Provider; profile=$Profile;
        python=$python; report=$reportPath; activates_environment=$false;
        model_loads=0; opens_camera=$false; opens_listener=$false } | ConvertTo-Json
    return
}
$result = $null
$passed = $false
$previousCuda = [Environment]::GetEnvironmentVariable('CUDA_VISIBLE_DEVICES', 'Process')
try {
    Assert-FlexGpuWorkerPython -Python $python
    $arguments = @((Join-Path $root 'tools/smoke_worker_runtime.py'), '--provider', $Provider)
    if ($RunGpuSmoke) {
        . (Join-Path $PSScriptRoot '_GeneratedGeometry.Common.ps1')
        $gpu = Assert-FlexGpuGeneratedGeometryProfile -Profile $Profile -GpuIndex $GpuIndex -NvidiaSmi $NvidiaSmi
        Assert-FlexGpuNoGeneratedGeometryWorker -RepositoryRoot $root
        $env:CUDA_VISIBLE_DEVICES = [string]$GpuIndex
        $arguments += @('--run', '--device', 'cuda:0')
        if ($Model) { $arguments += @('--model', $Model) }
    } else { $arguments += '--audit-only' }
    $output = & $python @arguments
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        $output | Write-Host
        throw "Candidate verification failed with exit code $code; no environment was activated."
    }
    $result = ($output -join [Environment]::NewLine) | ConvertFrom-Json
    if ($result.status -ne 'ok' -or $result.provider -ne $Provider) { throw 'Invalid candidate verification result.' }
    $passed = $true
} finally {
    if ($null -eq $previousCuda) { Remove-Item Env:CUDA_VISIBLE_DEVICES -ErrorAction SilentlyContinue }
    else { $env:CUDA_VISIBLE_DEVICES = $previousCuda }
    Write-FlexGpuLocalReport -Root $root -Path $reportPath -Value ([ordered]@{
        version='flexgpu-worker-candidate/v1'; status= $(if ($passed) { 'pass' } else { 'fail' });
        mode=$mode; provider=$Provider; profile=$Profile; python=$python;
        source=(Get-FlexGpuVerificationSource -Root $root);
        completed_utc=[DateTime]::UtcNow.ToString('o'); activated=$false;
        live_acceptance='not_tested'; thermal_acceptance='not_tested'; result=$result
    })
    Write-Host "[FlexGPU candidate] Local report: $reportPath"
}
Write-Host "[FlexGPU candidate] PASS ($mode). No environment activated; live/thermal checks are still required."

<#
.SYNOPSIS
Checks a machine-local live SHOW_CONTROL validation report.

.DESCRIPTION
Reads the ignored JSON report produced by touchdesigner\validate_show_controls.py
inside TouchDesigner. The check rejects failed controls, failed restoration,
the wrong GPU profile, and cooked wall or mosaic TOP dimensions that differ
from the requested venue output. Actual TOP width and height are authoritative;
this detects license-tier clamping even when resolution parameters still show
1920 by 1080. It also requires the complete 121-control public inventory and
the default-off desktop VR head/hand rehearsal checks. This does not validate
an OpenXR/OpenVR compositor or a physical headset.

This command is read-only. It does not start TouchDesigner, launch workers,
change Git state, or inspect private component internals.
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Report,

    [ValidateSet('3080ti_16gb', '4090_24gb', '5090_32gb')]
    [string]$ExpectedProfile = '3080ti_16gb'
)

$ErrorActionPreference = 'Stop'
$reportPath = (Resolve-Path -LiteralPath $Report -ErrorAction Stop).Path
$result = Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json

if ($result.version -ne 'flexgpu-show-controls-validation/v2') {
    throw "Unsupported SHOW_CONTROL report version: $($result.version)"
}
if ($result.profile -ne $ExpectedProfile) {
    throw (
        "SHOW_CONTROL report profile '$($result.profile)' does not match " +
        "expected profile '$ExpectedProfile'."
    )
}
if ($null -eq $result.output_dimensions) {
    throw 'SHOW_CONTROL report does not contain cooked output dimensions.'
}
if ($result.output_dimensions.status -ne 'pass') {
    $mismatches = @(
        $result.output_dimensions.mismatches.psobject.Properties |
            ForEach-Object {
                "$($_.Name): actual=$($_.Value.actual -join 'x') " +
                "expected=$($_.Value.expected -join 'x')"
            }
    )
    throw (
        "Cooked wall output dimensions do not match the requested venue " +
        "profile. $($mismatches -join '; ')"
    )
}
if ($result.status -ne 'pass' -or
    [int]$result.summary.fail -ne 0 -or
    [int]$result.summary.restoration_failures -ne 0) {
    throw 'SHOW_CONTROL report contains control or restoration failures.'
}

$valueControls = @($result.controls.value)
$pulseControls = @($result.controls.pulse)
$statusControls = @($result.controls.status)
if (
    $valueControls.Count -ne 101 -or
    $pulseControls.Count -ne 12 -or
    $statusControls.Count -ne 8
) {
    throw (
        'SHOW_CONTROL report does not contain the expected 121-control ' +
        "inventory: value=$($valueControls.Count), " +
        "pulse=$($pulseControls.Count), status=$($statusControls.Count)."
    )
}

$requiredVrValues = @(
    'Experience',
    'Vrinputsource',
    'Vrtargethz',
    'Vreyewidth',
    'Vreyeheight',
    'Vripdmetres',
    'Vrfovdegrees',
    'Vrheadxmetres',
    'Vrheadymetres',
    'Vrheadzmetres',
    'Vrheadyawdegrees',
    'Vrheadpitchdegrees',
    'Vrheadrolldegrees',
    'Vrhandenabled',
    'Vrhandgain',
    'Vrlefthandxmetres',
    'Vrlefthandymetres',
    'Vrlefthandzmetres',
    'Vrrighthandxmetres',
    'Vrrighthandymetres',
    'Vrrighthandzmetres'
)
$requiredVrPulses = @('Resetvrheadpose', 'Resetvrhands')
$requiredVrStatuses = @('Vrstatus')
$missingVrControls = @(
    $requiredVrValues | Where-Object { $_ -notin $valueControls }
    $requiredVrPulses | Where-Object { $_ -notin $pulseControls }
    $requiredVrStatuses | Where-Object { $_ -notin $statusControls }
)
if ($missingVrControls.Count -gt 0) {
    throw (
        'SHOW_CONTROL report is missing VR foundation controls: ' +
        ($missingVrControls -join ', ')
    )
}

$requiredVrChecks = @(
    'Experience_vr_enabled',
    'Experience_render_vr_enabled',
    'Vrinputsource',
    'Vrhandenabled',
    'Vrhandgain_shader',
    'Vrleft_eye_dimensions',
    'Vrright_eye_dimensions',
    'Resetvrheadpose_Vrheadxmetres',
    'Resetvrhands_Vrlefthandxmetres',
    'Experience_installation_vr_disabled'
)
$checksByName = @{}
foreach ($check in @($result.checks)) {
    $checksByName[[string]$check.name] = [string]$check.status
}
$failedVrChecks = @(
    $requiredVrChecks | Where-Object {
        -not $checksByName.ContainsKey($_) -or $checksByName[$_] -ne 'pass'
    }
)
if ($failedVrChecks.Count -gt 0) {
    throw (
        'SHOW_CONTROL report is missing or failed VR foundation checks: ' +
        ($failedVrChecks -join ', ')
    )
}

[ordered]@{
    status = 'ok'
    report = $reportPath
    profile = $result.profile
    controls_passed = [int]$result.summary.pass
    wall_width = [int]$result.output_dimensions.wall_width
    wall_height = [int]$result.output_dimensions.wall_height
    output_dimensions = $result.output_dimensions.status
    public_controls = (
        $valueControls.Count + $pulseControls.Count + $statusControls.Count)
    vr_foundation = 'desktop_mock_pass'
    headset_validation = 'not_performed'
    worker_buttons = $result.worker_buttons.status
    adapter_ui_buttons = $result.adapter_ui_buttons.status
} | ConvertTo-Json -Depth 5

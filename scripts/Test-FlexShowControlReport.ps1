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
the optional single-generator host's exact control/status inventory and wiring
gate when installed. Generator start/stop/offload pulses are never inferred
from a structural pass. It requires
the default-off desktop VR head/hand rehearsal checks when that feature is
installed. Older destinations may explicitly report wholly absent VR, Femto
trim and podcast scene-variant groups; partial groups still fail closed. No
feature is installed by this checker. Use canonical 4090/5090 profile names.
This does not validate
an OpenXR/OpenVR compositor or a physical headset.

This command is read-only. It does not start TouchDesigner, launch workers,
change Git state, or inspect private component internals.
An ok result accepts only the control mappings and cooked dimensions. It is
never full live-show acceptance: worker buttons, changing imagery, physical
sensors, headset submission, and a thermal soak require separate evidence.
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Report,

    [ValidateSet('3080ti_16gb', '4090', '5090')]
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
if ($result.acceptance_scope -ne 'public_control_mapping_and_output_dimensions' -or
    $result.complete_live_acceptance -ne $false) {
    throw 'SHOW_CONTROL report has an unsupported or overclaimed acceptance scope.'
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
$vrInstalled = $true
$femtoTrimInstalled = $true
$visualPathInstalled = $true
$vrValues = @(
    'Experience','Vrinputsource','Vrtargethz','Vreyewidth','Vreyeheight',
    'Vripdmetres','Vrfovdegrees','Vrheadxmetres','Vrheadymetres','Vrheadzmetres',
    'Vrheadyawdegrees','Vrheadpitchdegrees','Vrheadrolldegrees','Vrhandenabled',
    'Vrhandgain','Vrlefthandxmetres','Vrlefthandymetres','Vrlefthandzmetres',
    'Vrrighthandxmetres','Vrrighthandymetres','Vrrighthandzmetres'
)
$femtoTrimValues = @(
    'Femtomirrorhorizontal','Femtopositionscale','Femtotrimxmetres',
    'Femtotrimymetres','Femtotrimzmetres','Femtotrimyawdegrees',
    'Femtotrimpitchdegrees','Femtotrimrolldegrees',
    'Femtoaudiencenearmetres','Femtoaudiencefarmetres'
)
if ($null -ne $result.features) {
    if (@(Compare-Object @($result.features.psobject.Properties.Name) @(
        'vr_foundation','femto_calibration_trim','podcast_visual_path'
    )).Count -ne 0) {
        throw 'Unknown or missing optional feature inventory.'
    }
    foreach ($feature in @(
        @{ name='vr_foundation'; values=$vrValues; pulses=@('Resetvrheadpose','Resetvrhands'); statuses=@('Vrstatus') },
        @{ name='femto_calibration_trim'; values=$femtoTrimValues; pulses=@('Resetfemtocalibrationtrim'); statuses=@() }
    )) {
        $state = $result.features.($feature.name)
        if ($state -notin @('installed','not_installed')) {
            throw "Unknown optional feature state: $($feature.name)."
        }
        $expected = @($feature.values) + @($feature.pulses) + @($feature.statuses)
        $present = @($expected | Where-Object { $_ -in ($valueControls + $pulseControls + $statusControls) })
        if (($state -eq 'installed' -and $present.Count -ne $expected.Count) -or
            ($state -eq 'not_installed' -and ($present.Count -ne 0 -or $result.allow_absent_optional -ne $true))) {
            throw "Partially installed or unapproved absent optional feature: $($feature.name)."
        }
    }
    $vrInstalled = $result.features.vr_foundation -eq 'installed'
    $femtoTrimInstalled = $result.features.femto_calibration_trim -eq 'installed'
    $visualState = $result.features.podcast_visual_path
    $visualPresent = 'Visualpath' -in @($result.controls.adapter_value)
    if ($visualState -notin @('installed','not_installed') -or
        ($visualState -eq 'installed' -and -not $visualPresent) -or
        ($visualState -eq 'not_installed' -and ($visualPresent -or $result.allow_absent_optional -ne $true))) {
        throw 'Partially installed or unapproved absent podcast Visualpath feature.'
    }
    $visualPathInstalled = $visualState -eq 'installed'
}
$expectedValues = 101
$expectedPulses = 12
$expectedStatuses = 8
if (-not $vrInstalled) { $expectedValues -= 21; $expectedPulses -= 2; $expectedStatuses -= 1 }
if (-not $femtoTrimInstalled) { $expectedValues -= 10; $expectedPulses -= 1 }
if (
    $valueControls.Count -ne $expectedValues -or
    $pulseControls.Count -ne $expectedPulses -or
    $statusControls.Count -ne $expectedStatuses
) {
    throw (
        'SHOW_CONTROL report does not contain the expected installed-control ' +
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
if ($vrInstalled -and $missingVrControls.Count -gt 0) {
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
if ($vrInstalled -and $failedVrChecks.Count -gt 0) {
    throw (
        'SHOW_CONTROL report is missing or failed VR foundation checks: ' +
        ($failedVrChecks -join ', ')
    )
}

$hostReportStatus = 'not_installed'
$hostValues = @($result.controls.streamdiffusion_host_value)
$hostPulses = @($result.controls.streamdiffusion_host_pulse)
$hostStatuses = @($result.controls.streamdiffusion_host_status)
if (($hostValues + $hostPulses + $hostStatuses | Where-Object { $null -ne $_ }).Count -gt 0) {
    $expectedHostValues = @(
        'Sdsource','Sdsmoothenabled','Sdsmoothms','Sdtoxfile','Sdpayload',
        'Sdrgboutput','Sdfollowpodcast','Sdpromptformat','Sdpromptparameter',
        'Sdseedparameter','Sdstartparameter','Sdstopparameter','Sdstreamenabled',
        'Sdnoiseenabled','Sdnoisewidth','Sdnoiseheight','Sdnoiseseed',
        'Sdoffloadremove','Sdoffloadbackup'
    )
    $expectedHostPulses = @(
        'Sdreset','Sdloadtox','Sdstart','Sdstop','Sdopencontrols',
        'Sdopenpreview','Sdopennoisecontrols','Sdopennoisepreview','Sdoffload'
    )
    $expectedHostStatuses = @('Sdstatus','Sdstage','Sdoffloadstatus','Sdpromptstatus')
    foreach ($inventory in @(
        @{ actual = $hostValues; expected = $expectedHostValues },
        @{ actual = $hostPulses; expected = $expectedHostPulses },
        @{ actual = $hostStatuses; expected = $expectedHostStatuses }
    )) {
        if ($inventory.actual.Count -ne $inventory.expected.Count -or
            @(Compare-Object $inventory.actual $inventory.expected).Count -ne 0) {
            throw 'Single-generator host inventory is missing or stale; regenerate the live report.'
        }
    }
    if ($result.streamdiffusion_host.version -ne 'flexgpu-streamdiffusion-host-validation/v1' -or
        $result.streamdiffusion_host.status -ne 'pass' -or
        $checksByName['streamdiffusion_host_structure'] -ne 'pass' -or
        $result.streamdiffusion_host.complete_live_acceptance -ne $false -or
        $result.streamdiffusion_host.generator_buttons -ne 'external_validation_required') {
        throw 'Single-generator host structural gate failed or does not separate untested generator buttons.'
    }
    $hostReportStatus = 'structure_pass_buttons_not_tested'
}

[ordered]@{
    status = 'ok'
    acceptance_scope = 'public_control_mapping_and_output_dimensions'
    complete_live_acceptance = $false
    report = $reportPath
    profile = $result.profile
    controls_passed = [int]$result.summary.pass
    wall_width = [int]$result.output_dimensions.wall_width
    wall_height = [int]$result.output_dimensions.wall_height
    output_dimensions = $result.output_dimensions.status
    public_controls = (
        $valueControls.Count + $pulseControls.Count + $statusControls.Count)
    streamdiffusion_host_controls = ($hostValues.Count + $hostPulses.Count + $hostStatuses.Count)
    total_main_controls = ($valueControls.Count + $pulseControls.Count + $statusControls.Count +
        $hostValues.Count + $hostPulses.Count + $hostStatuses.Count)
    vr_foundation = $(if ($vrInstalled) { 'desktop_mock_pass' } else { 'not_installed' })
    femto_calibration_trim = $(if ($femtoTrimInstalled) { 'installed' } else { 'not_installed' })
    podcast_visual_path = $(if ($visualPathInstalled) { 'installed' } else { 'not_installed' })
    headset_validation = 'not_performed'
    worker_buttons = $result.worker_buttons.status
    adapter_ui_buttons = $result.adapter_ui_buttons.status
    streamdiffusion_host = $hostReportStatus
} | ConvertTo-Json -Depth 5

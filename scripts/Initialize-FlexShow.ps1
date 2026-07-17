#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('auto', 'single', 'dual_local')]
    [string]$Topology = 'auto',

    [ValidateSet('installation', 'vr', 'combined')]
    [string]$Experience = 'installation',

    [ValidateSet('fog', 'procedural', 'hybrid')]
    [string]$Completion = 'hybrid',

    [int]$AIIndex = -1,

    [int]$RenderIndex = -1,

    [string]$Output = 'config/local-flexshow.json',

    [string]$Project = '',

    [string]$TouchDesignerExe = '',

    [ValidatePattern('^20\d{2}\.\d{5}$')]
    [string]$TouchDesignerVersion = '',

    [string]$NvidiaSmi = '',

    [switch]$ListOnly,

    [switch]$ListTouchDesigner,

    [switch]$Force,

    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($env:OS -ne 'Windows_NT') {
    throw 'Initialize-FlexShow.ps1 currently supports Windows only.'
}

$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$validatedTouchDesignerVersion = '2025.32820'
if ($AIIndex -lt -1 -or $RenderIndex -lt -1) {
    throw 'AIIndex and RenderIndex must be -1 (automatic) or a non-negative GPU index.'
}

function Find-NvidiaSmiExecutable {
    param([string]$RequestedPath)

    if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
        $resolved = [System.IO.Path]::GetFullPath($RequestedPath)
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            throw "nvidia-smi does not exist: $resolved"
        }
        return $resolved
    }

    $command = Get-Command 'nvidia-smi.exe' -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }
    $windowsRoot = if ([string]::IsNullOrWhiteSpace($env:WINDIR)) { 'C:\Windows' } else { $env:WINDIR }
    $systemPath = Join-Path $windowsRoot 'System32\nvidia-smi.exe'
    if (Test-Path -LiteralPath $systemPath -PathType Leaf) {
        return $systemPath
    }
    throw 'nvidia-smi was not found. Install an NVIDIA driver or pass -NvidiaSmi.'
}

function Get-NvidiaGpuInventory {
    param([string]$Executable)

    $query = '--query-gpu=index,uuid,pci.bus_id,name,memory.total,driver_version'
    $format = '--format=csv,noheader,nounits'
    $raw = & $Executable $query $format
    if ($LASTEXITCODE -ne 0) {
        throw "nvidia-smi exited with code $LASTEXITCODE."
    }
    $headers = @('index', 'uuid', 'bus_id', 'name', 'memory_total_mib', 'driver_version')
    $rows = @($raw | ConvertFrom-Csv -Header $headers)
    $gpus = [System.Collections.Generic.List[object]]::new()
    foreach ($row in $rows) {
        $memoryMatch = [regex]::Match([string]$row.memory_total_mib, '\d+(?:\.\d+)?')
        $parsedIndex = 0
        if (-not [int]::TryParse(([string]$row.index).Trim(), [ref]$parsedIndex) -or -not $memoryMatch.Success) {
            throw "Unable to parse nvidia-smi GPU row: $($row | ConvertTo-Json -Compress)"
        }
        $memory = [int][math]::Round(
            [double]::Parse($memoryMatch.Value, [Globalization.CultureInfo]::InvariantCulture)
        )
        $gpus.Add([pscustomobject]@{
            Index = $parsedIndex
            Uuid = ([string]$row.uuid).Trim()
            BusId = ([string]$row.bus_id).Trim().ToUpperInvariant()
            Name = ([string]$row.name).Trim()
            MemoryTotalMiB = $memory
            DriverVersion = ([string]$row.driver_version).Trim()
        })
    }
    if ($gpus.Count -eq 0) {
        throw 'nvidia-smi reported no NVIDIA GPUs.'
    }
    return @($gpus | Sort-Object -Property Index)
}

function Get-GpuByIndex {
    param(
        [object[]]$Gpus,
        [int]$Index,
        [string]$Role
    )

    $matches = @($Gpus | Where-Object { $_.Index -eq $Index })
    if ($matches.Count -ne 1) {
        throw "$Role GPU index $Index was not reported by nvidia-smi."
    }
    return $matches[0]
}

function Get-HardwareTier {
    param([object]$Gpu)

    $normalized = ([regex]::Replace($Gpu.Name.ToUpperInvariant(), '[^A-Z0-9]+', ' ')).Trim()
    if ($normalized.Contains('RTX 5090') -and $Gpu.MemoryTotalMiB -ge 28000) {
        return '5090'
    }
    if ($normalized.Contains('RTX 4090') -and -not $normalized.Contains('LAPTOP') -and $Gpu.MemoryTotalMiB -ge 20000) {
        return '4090'
    }
    if (
        $normalized.Contains('RTX 3080 TI') -and
        $normalized.Contains('LAPTOP') -and
        $Gpu.MemoryTotalMiB -ge 15000 -and
        $Gpu.MemoryTotalMiB -le 17500
    ) {
        return '3080ti_16gb'
    }
    # Keep the generated file compatible with the public schema. Runtime auto
    # classification will choose conservative custom settings for unknown GPUs.
    return 'auto'
}

function Get-TouchDesignerVersion {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable
    )

    $productVersion = ''
    try {
        $versionInfo = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($Executable)
        $productVersion = [string]$versionInfo.ProductVersion
    }
    catch {
        # A metadata-less synthetic executable is valid for release smoke tests.
        $productVersion = ''
    }

    $binDirectory = [System.IO.Path]::GetDirectoryName($Executable)
    $installDirectory = if ([string]::IsNullOrWhiteSpace($binDirectory)) {
        ''
    }
    else {
        [System.IO.Path]::GetDirectoryName($binDirectory)
    }
    $installFolder = if ([string]::IsNullOrWhiteSpace($installDirectory)) {
        ''
    }
    else {
        [System.IO.Path]::GetFileName($installDirectory)
    }

    foreach ($value in @($productVersion, $installFolder)) {
        $match = [regex]::Match([string]$value, '(?<!\d)(20\d{2}\.\d{5})(?!\d)')
        if ($match.Success) {
            return $match.Groups[1].Value
        }
    }
    return 'unknown'
}

function Get-TouchDesignerInstallations {
    $rawCandidates = [System.Collections.Generic.List[object]]::new()
    $derivativeRoot = Join-Path $env:ProgramFiles 'Derivative'
    $standard = Join-Path $derivativeRoot 'TouchDesigner\bin\TouchDesigner.exe'
    if (Test-Path -LiteralPath $standard -PathType Leaf) {
        $rawCandidates.Add([pscustomobject]@{
            path = $standard
            source = 'standard'
            is_standard = $true
        })
    }

    $pathCommand = Get-Command 'TouchDesigner.exe' -ErrorAction SilentlyContinue
    if ($null -ne $pathCommand) {
        $rawCandidates.Add([pscustomobject]@{
            path = $pathCommand.Source
            source = 'PATH'
            is_standard = $false
        })
    }

    if (Test-Path -LiteralPath $derivativeRoot -PathType Container) {
        foreach ($folder in Get-ChildItem -LiteralPath $derivativeRoot -Directory -Filter 'TouchDesigner*' |
            Sort-Object -Property FullName) {
            $candidate = Join-Path $folder.FullName 'bin\TouchDesigner.exe'
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                $rawCandidates.Add([pscustomobject]@{
                    path = $candidate
                    source = 'side_by_side'
                    is_standard = $false
                })
            }
        }
    }

    $seen = @{}
    $inventory = [System.Collections.Generic.List[object]]::new()
    foreach ($candidate in $rawCandidates) {
        $resolved = [System.IO.Path]::GetFullPath([string]$candidate.path)
        if ($seen.ContainsKey($resolved)) {
            continue
        }
        $seen[$resolved] = $true
        $inventory.Add([pscustomobject]@{
            path = $resolved
            version = Get-TouchDesignerVersion -Executable $resolved
            source = [string]$candidate.source
            is_standard = [bool]$candidate.is_standard
        })
    }

    $sortProperties = @(
        @{ Expression = { if ($_.is_standard) { 0 } else { 1 } }; Descending = $false },
        @{ Expression = 'version'; Descending = $true },
        @{ Expression = 'path'; Descending = $false }
    )
    return @($inventory | Sort-Object -Property $sortProperties)
}

function Select-TouchDesignerInstallation {
    param(
        [string]$RequestedPath,
        [string]$RequestedVersion
    )

    if (-not [string]::IsNullOrWhiteSpace($RequestedPath) -and
        -not [string]::IsNullOrWhiteSpace($RequestedVersion)) {
        throw '-TouchDesignerExe and -TouchDesignerVersion cannot be combined. Use one exact selector.'
    }

    if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
        $resolved = [System.IO.Path]::GetFullPath($RequestedPath)
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            throw "TouchDesigner executable does not exist: $resolved"
        }
        return [pscustomobject]@{
            path = $resolved
            version = Get-TouchDesignerVersion -Executable $resolved
            selection = 'explicit_path'
        }
    }

    $installations = @(Get-TouchDesignerInstallations)
    if (-not [string]::IsNullOrWhiteSpace($RequestedVersion)) {
        $matches = @($installations | Where-Object { $_.version -eq $RequestedVersion })
        if ($matches.Count -eq 0) {
            $available = @($installations | Where-Object { $_.version -ne 'unknown' } |
                ForEach-Object { $_.version } | Sort-Object -Unique) -join ', '
            if ([string]::IsNullOrWhiteSpace($available)) {
                $available = 'none detected'
            }
            throw "TouchDesigner version $RequestedVersion was not found. Installed versions: $available. Run -ListTouchDesigner or pass -TouchDesignerExe."
        }
        if ($matches.Count -ne 1) {
            throw "TouchDesigner version $RequestedVersion has multiple executable paths. Pass -TouchDesignerExe to select one exactly."
        }
        return [pscustomobject]@{
            path = $matches[0].path
            version = $matches[0].version
            selection = 'explicit_version'
        }
    }

    $validatedMatches = @($installations | Where-Object {
        $_.version -eq $validatedTouchDesignerVersion
    })
    if ($validatedMatches.Count -gt 1) {
        $validatedStandardMatches = @($validatedMatches | Where-Object { $_.is_standard })
        if ($validatedStandardMatches.Count -eq 1) {
            $validatedMatches = @($validatedStandardMatches[0])
        }
    }
    if ($validatedMatches.Count -eq 1) {
        return [pscustomobject]@{
            path = $validatedMatches[0].path
            version = $validatedMatches[0].version
            selection = 'validated_baseline'
        }
    }
    if ($installations.Count -eq 0) {
        throw 'TouchDesigner.exe was not found. Install TouchDesigner or pass -TouchDesignerExe.'
    }
    throw "The validated TouchDesigner baseline $validatedTouchDesignerVersion was not found uniquely. Run -ListTouchDesigner and pass -TouchDesignerVersion or -TouchDesignerExe to select another build explicitly."
}

function Resolve-LocalConfigOutput {
    param([string]$RequestedPath)

    $fullPath = if ([System.IO.Path]::IsPathRooted($RequestedPath)) {
        [System.IO.Path]::GetFullPath($RequestedPath)
    }
    else {
        [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot $RequestedPath))
    }
    $configRoot = [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot 'config'))
    $parent = [System.IO.Path]::GetDirectoryName($fullPath)
    if (-not $parent.Equals($configRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Local preset must be written directly under the repository config directory: $configRoot"
    }
    $leaf = [System.IO.Path]::GetFileName($fullPath)
    if ($leaf -notlike 'local-*.json' -and $leaf -notlike '*-local.json') {
        throw 'Local preset filename must match local-*.json or *-local.json so .gitignore protects it.'
    }
    return $fullPath
}

function Resolve-TouchDesignerProject {
    param([string]$RequestedPath)

    $fullPath = if ([string]::IsNullOrWhiteSpace($RequestedPath)) {
        [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot 'projects\FlexShow.toe'))
    }
    elseif ([System.IO.Path]::IsPathRooted($RequestedPath)) {
        [System.IO.Path]::GetFullPath($RequestedPath)
    }
    else {
        [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot $RequestedPath))
    }
    if (-not [System.IO.Path]::GetExtension($fullPath).Equals(
        '.toe',
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "TouchDesigner project must be a .toe file: $fullPath"
    }
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        throw "TouchDesigner project does not exist: $fullPath"
    }
    return $fullPath
}

if ($ListOnly -and $ListTouchDesigner) {
    throw '-ListOnly and -ListTouchDesigner cannot be combined.'
}
if ($ListTouchDesigner) {
    if (-not [string]::IsNullOrWhiteSpace($TouchDesignerExe) -or
        -not [string]::IsNullOrWhiteSpace($TouchDesignerVersion)) {
        throw '-ListTouchDesigner cannot be combined with a TouchDesigner selector.'
    }
    $installations = @(Get-TouchDesignerInstallations)
    $validatedMatches = @($installations | Where-Object {
        $_.version -eq $validatedTouchDesignerVersion
    })
    if ($validatedMatches.Count -gt 1) {
        $validatedStandardMatches = @($validatedMatches | Where-Object { $_.is_standard })
        if ($validatedStandardMatches.Count -eq 1) {
            $validatedMatches = @($validatedStandardMatches[0])
        }
    }
    $defaultPath = if ($validatedMatches.Count -eq 1) {
        [string]$validatedMatches[0].path
    } else { '' }
    $rows = @($installations | ForEach-Object {
        [pscustomobject][ordered]@{
            version = $_.version
            default = (-not [string]::IsNullOrWhiteSpace($defaultPath) -and
                [string]::Equals($_.path, $defaultPath, [System.StringComparison]::OrdinalIgnoreCase))
            is_standard = $_.is_standard
            source = $_.source
            path = $_.path
        }
    })
    if ($Json) {
        ConvertTo-Json -InputObject $rows -Depth 4 -Compress
    }
    elseif ($rows.Count -eq 0) {
        Write-Host '[FlexShow] no TouchDesigner installations found.'
    }
    else {
        $rows | Format-Table -AutoSize version, default, is_standard, source, path
    }
    return
}

$nvidiaSmiPath = Find-NvidiaSmiExecutable -RequestedPath $NvidiaSmi
$gpus = @(Get-NvidiaGpuInventory -Executable $nvidiaSmiPath)

if ($ListOnly) {
    if ($Json) {
        $inventory = @($gpus | ForEach-Object {
            [ordered]@{
                index = $_.Index
                uuid = $_.Uuid
                bus_id = $_.BusId
                name = $_.Name
                memory_total_mib = $_.MemoryTotalMiB
                driver_version = $_.DriverVersion
                tier = Get-HardwareTier -Gpu $_
            }
        })
        ConvertTo-Json -InputObject $inventory -Depth 4
    }
    else {
        $gpus | Format-Table -AutoSize Index, Name, MemoryTotalMiB, Uuid, BusId
    }
    return
}

$resolvedTopology = if ($Topology -eq 'auto') {
    if ($gpus.Count -ge 2) { 'dual_local' } else { 'single' }
}
else {
    $Topology
}
if ($resolvedTopology -eq 'dual_local' -and $gpus.Count -lt 2) {
    throw 'dual_local requires at least two NVIDIA GPUs.'
}

$explicitAi = if ($AIIndex -ge 0) { Get-GpuByIndex -Gpus $gpus -Index $AIIndex -Role 'AI' } else { $null }
$explicitRender = if ($RenderIndex -ge 0) {
    Get-GpuByIndex -Gpus $gpus -Index $RenderIndex -Role 'render'
}
else {
    $null
}
$ranked = @($gpus | Sort-Object -Property @{ Expression = 'MemoryTotalMiB'; Descending = $true }, Index)

if ($resolvedTopology -eq 'single') {
    if ($null -ne $explicitAi -and $null -ne $explicitRender -and $explicitAi.Index -ne $explicitRender.Index) {
        throw 'single topology cannot assign AI and render to different GPUs.'
    }
    $renderGpu = if ($null -ne $explicitRender) { $explicitRender } elseif ($null -ne $explicitAi) { $explicitAi } else { $ranked[0] }
    $aiGpu = $renderGpu
}
else {
    if ($null -ne $explicitAi -and $null -ne $explicitRender -and $explicitAi.Index -eq $explicitRender.Index) {
        throw 'dual_local requires distinct AI and render GPU indices.'
    }
    $aiGpu = if ($null -ne $explicitAi) {
        $explicitAi
    }
    else {
        @($ranked | Where-Object { $null -eq $explicitRender -or $_.Index -ne $explicitRender.Index })[0]
    }
    $renderGpu = if ($null -ne $explicitRender) {
        $explicitRender
    }
    else {
        @($ranked | Where-Object { $_.Index -ne $aiGpu.Index })[0]
    }
}

$touchDesignerSelection = Select-TouchDesignerInstallation `
    -RequestedPath $TouchDesignerExe `
    -RequestedVersion $TouchDesignerVersion
$touchDesignerPath = $touchDesignerSelection.path
$projectPath = Resolve-TouchDesignerProject -RequestedPath $Project
$outputPath = Resolve-LocalConfigOutput -RequestedPath $Output
if ((Test-Path -LiteralPath $outputPath) -and -not $Force) {
    throw "Local preset already exists: $outputPath. Pass -Force to replace it."
}

$aiTier = Get-HardwareTier -Gpu $aiGpu
$renderTier = Get-HardwareTier -Gpu $renderGpu
# In a heterogeneous local pair the launcher resolves quality independently
# for each assigned process.  Persisting the AI GPU's tier here would apply its
# larger point/render limits to the weaker world GPU.
$tier = if ($resolvedTopology -eq 'dual_local') { 'auto' } else { $renderTier }
$atlasFps = switch ($aiTier) {
    '5090' { 15 }
    '4090' { 10 }
    default { 5 }
}
$worldProcess = [ordered]@{
    executable = $touchDesignerPath
    project = $projectPath
    cwd = $repositoryRoot
    touchdesigner = $true
}
$processes = [ordered]@{ world = $worldProcess }
if ($resolvedTopology -eq 'dual_local') {
    $processes = [ordered]@{
        ai = [ordered]@{
            executable = $touchDesignerPath
            project = $projectPath
            cwd = $repositoryRoot
            touchdesigner = $true
        }
        world = $worldProcess
    }
}
$transportType = if ($resolvedTopology -eq 'dual_local') { 'touch_tcp' } else { 'local' }
$transport = [ordered]@{
    type = $transportType
    atlas_width = 1024
    atlas_height = 512
    atlas_fps = $atlasFps
    atlas_port = 12000
    control_port = 12001
    heartbeat_port = 12002
    heartbeat_timeout_ms = 2000
}
if ($resolvedTopology -eq 'dual_local') {
    # Touch In exposes num_received_frames, which provides a usable turnkey
    # transport-arrival counter. Shared Mem remains an advanced integration
    # because it needs an explicit producer-backed frame-state sidecar.
    $transport.peer_host = '127.0.0.1'
}
$configuration = [ordered]@{
    '$schema' = './flexshow.schema.json'
    topology = $resolvedTopology
    experience = $Experience
    completion = $Completion
    tier = $tier
    gpu = [ordered]@{
        ai = [ordered]@{ uuid = $aiGpu.Uuid }
        render = [ordered]@{ uuid = $renderGpu.Uuid }
    }
    processes = $processes
    transport = $transport
    runtime_dir = [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot '.flexgpu\local'))
}

if ($PSCmdlet.ShouldProcess($outputPath, 'Write machine-local FlexShow preset')) {
    $content = $configuration | ConvertTo-Json -Depth 12
    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($outputPath, $content + [Environment]::NewLine, $utf8WithoutBom)
}

$result = [ordered]@{
    status = 'ok'
    config = $outputPath
    topology = $resolvedTopology
    tier = $tier
    ai_tier = $aiTier
    render_tier = $renderTier
    ai_gpu = [ordered]@{ index = $aiGpu.Index; uuid = $aiGpu.Uuid; name = $aiGpu.Name }
    render_gpu = [ordered]@{ index = $renderGpu.Index; uuid = $renderGpu.Uuid; name = $renderGpu.Name }
    touchdesigner = $touchDesignerPath
    touchdesigner_version = $touchDesignerSelection.version
    touchdesigner_selection = $touchDesignerSelection.selection
    project = $projectPath
}
if ($Json) {
    $result | ConvertTo-Json -Depth 5 -Compress
}
else {
    Write-Host "[FlexShow] local preset: $outputPath"
    Write-Host "[FlexShow] topology=$resolvedTopology tier=$tier AI-tier=$aiTier render-tier=$renderTier"
    Write-Host "[FlexShow] AI GPU $($aiGpu.Index): $($aiGpu.Name)"
    Write-Host "[FlexShow] render GPU $($renderGpu.Index): $($renderGpu.Name)"
    Write-Host "[FlexShow] TouchDesigner $($touchDesignerSelection.version) [$($touchDesignerSelection.selection)]: $touchDesignerPath"
}

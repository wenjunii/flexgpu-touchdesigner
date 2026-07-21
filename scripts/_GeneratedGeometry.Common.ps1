#Requires -Version 5.1
Set-StrictMode -Version Latest

function Get-FlexGpuGeneratedGeometryWorkers {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryRoot
    )

    $workerPath = [System.IO.Path]::GetFullPath(
        (Join-Path $RepositoryRoot 'tools\moge2_worker.py'))
    $escapedWorkerPath = [regex]::Escape($workerPath)
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop |
        Where-Object {
            -not [string]::IsNullOrWhiteSpace($_.CommandLine) -and
            $_.CommandLine -match $escapedWorkerPath -and
            $_.CommandLine -match '--backend\s+(moge2|depth_anything)(?:\s|$)'
        })

    foreach ($processInfo in $processes) {
        $provider = if ($processInfo.CommandLine -match '--backend\s+depth_anything(?:\s|$)') {
            'depth_anything'
        }
        else {
            'moge2'
        }
        [pscustomobject]@{
            ProcessId = [int]$processInfo.ProcessId
            ParentProcessId = [int]$processInfo.ParentProcessId
            Name = [string]$processInfo.Name
            Provider = $provider
            WorkerPath = $workerPath
        }
    }
}

function Assert-FlexGpuNoGeneratedGeometryWorker {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryRoot
    )

    $running = @(Get-FlexGpuGeneratedGeometryWorkers -RepositoryRoot $RepositoryRoot)
    if ($running.Count -eq 0) {
        return
    }

    $summary = ($running |
        Sort-Object Provider, ProcessId |
        ForEach-Object { "$($_.Provider):PID$($_.ProcessId)" }) -join ', '
    throw "A generated-geometry worker from this checkout is already running ($summary). Stop it with scripts\Stop-GeneratedGeometryWorker.ps1 -Stop before starting another provider."
}

function Resolve-FlexGpuNvidiaSmi {
    param(
        [AllowEmptyString()]
        [string]$NvidiaSmi = ''
    )

    if (-not [string]::IsNullOrWhiteSpace($NvidiaSmi)) {
        $resolved = [System.IO.Path]::GetFullPath($NvidiaSmi)
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            throw "nvidia-smi does not exist: $resolved"
        }
        return $resolved
    }

    $command = Get-Command nvidia-smi.exe -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $command) {
        throw 'nvidia-smi is required to verify the requested worker profile.'
    }
    return $command.Source
}

function Assert-FlexGpuGeneratedGeometryProfile {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('3080ti_16gb', '4090', '5090')]
        [string]$Profile,

        [Parameter(Mandatory = $true)]
        [ValidateRange(0, 31)]
        [int]$GpuIndex,

        [AllowEmptyString()]
        [string]$NvidiaSmi = '',

        [switch]$AllowProfileMismatch
    )

    $executable = Resolve-FlexGpuNvidiaSmi -NvidiaSmi $NvidiaSmi
    $rows = @(& $executable `
        '--query-gpu=index,name,memory.total' `
        '--format=csv,noheader,nounits')
    if ($LASTEXITCODE -ne 0) {
        throw "nvidia-smi profile query failed with code $LASTEXITCODE."
    }

    $selected = $null
    foreach ($row in $rows) {
        $parts = @([string]$row -split '\s*,\s*', 3)
        if ($parts.Count -ne 3) {
            continue
        }
        $rowIndex = 0
        if (-not [int]::TryParse($parts[0].Trim(), [ref]$rowIndex)) {
            continue
        }
        if ($rowIndex -ne $GpuIndex) {
            continue
        }
        $memoryMiB = [int][double]::Parse(
            $parts[2].Trim(),
            [System.Globalization.CultureInfo]::InvariantCulture)
        $selected = [pscustomobject]@{
            index = $rowIndex
            name = $parts[1].Trim()
            memory_mib = $memoryMiB
        }
        break
    }
    if ($null -eq $selected) {
        throw "Physical GPU index $GpuIndex was not reported by nvidia-smi."
    }

    $matches = switch ($Profile) {
        '3080ti_16gb' {
            $selected.name -match '(?i)\bRTX\s+3080\s+Ti\b' -and
                $selected.memory_mib -ge 14000
        }
        '4090' {
            $selected.name -match '(?i)\bRTX\s+4090\b' -and
                $selected.memory_mib -ge 22000
        }
        '5090' {
            $selected.name -match '(?i)\bRTX\s+5090\b' -and
                $selected.memory_mib -ge 30000
        }
    }

    if (-not $matches) {
        $message = "Worker profile '$Profile' does not match GPU $GpuIndex '$($selected.name)' with $($selected.memory_mib) MiB. Keep 3080 and 5090 launch profiles separate."
        if (-not $AllowProfileMismatch) {
            throw "$message Use -AllowProfileMismatch only for an intentional, separately validated override."
        }
        Write-Warning $message
    }

    return $selected
}

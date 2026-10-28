# Keep machine-local evidence under ignored runtime/, including failure reports.
function Get-FlexGpuVerificationSource {
    param([string]$Root)
    try {
        $commit = @(& git -C $Root rev-parse HEAD 2>$null)[-1]
        if ($LASTEXITCODE -ne 0) { throw 'No Git revision.' }
        $dirty = @(& git -C $Root status --porcelain 2>$null)
        if ($LASTEXITCODE -ne 0) { throw 'No Git status.' }
        return @{ commit=[string]$commit; dirty=[bool]($dirty.Count -gt 0) }
    } catch { return @{ commit=$null; dirty=$null } }
}

function Resolve-FlexGpuLocalReportPath {
    param([string]$Root, [string]$Path)
    $runtime = [IO.Path]::GetFullPath((Join-Path $Root 'runtime'))
    $target = if ([IO.Path]::IsPathRooted($Path)) {
        [IO.Path]::GetFullPath($Path)
    } else { [IO.Path]::GetFullPath((Join-Path $Root $Path)) }
    if (-not $target.StartsWith($runtime + [IO.Path]::DirectorySeparatorChar,
            [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetExtension($target) -ne '.json') {
        throw 'Verification reports must be JSON files inside this checkout''s ignored runtime directory.'
    }
    $cursor = $target
    while ($cursor -and $cursor.Length -ge $runtime.Length) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Verification report paths must not traverse a symlink or junction.'
            }
        }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
    return $target
}

function Write-FlexGpuLocalReport {
    param([string]$Root, [string]$Path, [object]$Value)
    $target = Resolve-FlexGpuLocalReportPath -Root $Root -Path $Path
    New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($target)) -Force | Out-Null
    $temporary = $target + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    try {
        $json = $Value | ConvertTo-Json -Depth 12
        [IO.File]::WriteAllText($temporary, $json, (New-Object Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $temporary -Destination $target -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

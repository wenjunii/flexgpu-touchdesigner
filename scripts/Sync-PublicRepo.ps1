<#
.SYNOPSIS
Safely stages, commits, and optionally pushes the public FlexShow repository.

.DESCRIPTION
Without switches this script is read-only. With -Stage it checks all candidate
files before git add and checks the exact index afterward. -Commit requires a
message. -Push is always explicit and refuses a dirty or staged worktree.

Because a compressed .toe can hide embedded private components, changing the
canonical projects/FlexShow.toe also requires -AllowCanonicalProjectUpdate.
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Stage,

    [switch]$Commit,

    [string]$Message = '',

    [switch]$Push,

    [string]$Remote = 'origin',

    [switch]$AllowCanonicalProjectUpdate
)

. (Join-Path $PSScriptRoot '_FlexShow.Common.ps1')

$root = Get-FlexShowRepositoryRoot
$checker = Join-Path $PSScriptRoot 'Test-PublicSync.ps1'
$gitCommand = Get-Command git -ErrorAction SilentlyContinue
if ($null -eq $gitCommand) {
    throw 'Git is required for public repository synchronization.'
}
if ($Remote -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') {
    throw 'Remote must be a conservative Git remote name, not a URL or option.'
}
if ($Commit -and [string]::IsNullOrWhiteSpace($Message)) {
    throw '-Commit requires a non-empty -Message.'
}

function Invoke-CheckedGit {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [int[]]$AllowedExitCodes = @(0),

        [switch]$DiscardOutput,

        [string]$Operation = 'Git operation'
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $hadNativePreference = Test-Path Variable:PSNativeCommandUseErrorActionPreference
    $previousNativePreference = $null
    if ($hadNativePreference) {
        $previousNativePreference = $PSNativeCommandUseErrorActionPreference
        $PSNativeCommandUseErrorActionPreference = $false
    }
    try {
        $ErrorActionPreference = 'Continue'
        if ($DiscardOutput) {
            & $gitCommand.Source -C $root @Arguments *> $null
        }
        else {
            & $gitCommand.Source -C $root @Arguments
        }
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        if ($hadNativePreference) {
            $PSNativeCommandUseErrorActionPreference = $previousNativePreference
        }
    }
    if ($AllowedExitCodes -notcontains $exitCode) {
        # Arguments can contain a commit message or path. Never echo them.
        throw "$Operation failed with code $exitCode. Native output was suppressed when sensitive."
    }
    return $exitCode
}

function Test-CurrentPublicSurface {
    # Candidate/index scans cover exactly what git add can publish. Limiting
    # history to HEAD avoids treating an unrelated local stash or private branch
    # as part of this push. CI scans every branch and tag in the public clone.
    & $checker -Scope Both
    & $checker -Scope History -Revision HEAD
}

function Test-CanonicalProjectChanged {
    param([switch]$IncludeUnpushed)

    $working = & $gitCommand.Source -C $root status --porcelain=v1 --untracked-files=all -- projects/FlexShow.toe
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not inspect canonical TouchDesigner project status.'
    }
    $staged = & $gitCommand.Source -C $root diff --cached --name-only -- projects/FlexShow.toe
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not inspect staged TouchDesigner project status.'
    }
    $changed = -not ([string]::IsNullOrWhiteSpace(($working -join "`n")) -and
                    [string]::IsNullOrWhiteSpace(($staged -join "`n")))
    if ($IncludeUnpushed) {
        $branch = (& $gitCommand.Source -C $root branch --show-current).Trim()
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($branch)) {
            throw 'Cannot inspect unpushed canonical changes from detached HEAD.'
        }
        if ($branch -notmatch '^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$' -or
            $branch.Contains('..') -or $branch.Contains('@{') -or
            $branch.EndsWith('/') -or $branch.EndsWith('.')) {
            throw 'Current branch name is not safe for guarded public sync.'
        }
        # Keep checker diagnostics visible without adding them to this
        # function's Boolean success-stream return value.
        & $checker -Scope Index -InputText $branch -InputLabel 'branch-name' | Out-Host
        $remoteReference = "refs/remotes/$Remote/$branch"
        & $gitCommand.Source -C $root show-ref --verify --quiet $remoteReference
        $remoteRefCode = $LASTEXITCODE
        if ($remoteRefCode -eq 0) {
            # Inspect every unpushed commit, not only endpoint trees. A private
            # .toe added and then reverted would otherwise escape acknowledgement.
            $unpushed = & $gitCommand.Source -C $root rev-list "$remoteReference..HEAD" -- projects/FlexShow.toe
            if ($LASTEXITCODE -ne 0) {
                throw 'Could not inspect unpushed canonical TouchDesigner changes.'
            }
            $changed = $changed -or -not [string]::IsNullOrWhiteSpace(($unpushed -join "`n"))
        }
        elseif ($remoteRefCode -ne 1) {
            throw 'Could not inspect the remote-tracking branch for canonical changes.'
        }
        elseif (-not $AllowCanonicalProjectUpdate) {
            throw 'Remote-tracking branch is unavailable; use -AllowCanonicalProjectUpdate after manual .toe inspection.'
        }
    }
    return $changed
}

& $checker -Scope Index -InputText $Remote -InputLabel 'remote-name'
$canonicalChanged = Test-CanonicalProjectChanged -IncludeUnpushed:$Push
if ($canonicalChanged -and -not $AllowCanonicalProjectUpdate -and ($Stage -or $Commit -or $Push)) {
    throw @'
The canonical projects/FlexShow.toe changed. A .toe can embed private .tox
content that text scanners cannot reliably inspect. Verify the public project
contains no StreamDiffusionTD component, credentials, paid assets, or private
paths, then rerun with -AllowCanonicalProjectUpdate.
'@
}
elseif ($canonicalChanged -and -not ($Stage -or $Commit -or $Push)) {
    Write-Warning 'Canonical projects/FlexShow.toe changed; publishing it will require -AllowCanonicalProjectUpdate after manual inspection.'
}

# Scan the working candidates and existing index before any mutation. Ignored
# private files are absent from the candidate list and cannot be staged by the
# normal git add performed below.
Test-CurrentPublicSurface

if (-not ($Stage -or $Commit -or $Push)) {
    Write-Host '[FlexShow] Read-only public-sync preview passed. Add -Stage, -Commit, and -Push explicitly.'
    Invoke-CheckedGit -Arguments @('status', '--short') -DiscardOutput -Operation 'Git status check' | Out-Null
    return
}

if ($Stage) {
    Write-Host '[FlexShow] Staging all non-ignored repository changes.'
    Invoke-CheckedGit -Arguments @('add', '-A', '--', '.') -DiscardOutput -Operation 'Git staging' | Out-Null
    & $checker -Scope Index
}
else {
    & $checker -Scope Index
}

if ($Commit) {
    & $checker -Scope Index -InputText $Message -InputLabel 'commit-message'
    $diffCode = Invoke-CheckedGit -Arguments @('diff', '--cached', '--quiet', '--exit-code') -AllowedExitCodes @(0, 1) -DiscardOutput -Operation 'Staged-change check'
    if ($diffCode -eq 0) {
        throw 'No staged changes are available to commit.'
    }
    Write-Host '[FlexShow] Committing the policy-checked index.'
    Invoke-CheckedGit -Arguments @('commit', '-m', $Message) -DiscardOutput -Operation 'Git commit' | Out-Null
}

if ($Push) {
    $indexCode = Invoke-CheckedGit -Arguments @('diff', '--cached', '--quiet', '--exit-code') -AllowedExitCodes @(0, 1) -DiscardOutput -Operation 'Staged-change check'
    if ($indexCode -ne 0) {
        throw 'Push refused: staged changes have not been committed.'
    }
    $status = & $gitCommand.Source -C $root status --porcelain=v1 --untracked-files=normal
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not verify repository cleanliness before push.'
    }
    if (-not [string]::IsNullOrWhiteSpace(($status -join "`n"))) {
        throw 'Push refused: the worktree is not clean. Stage and commit the intended public changes first.'
    }
    Test-CurrentPublicSurface
    $branch = (& $gitCommand.Source -C $root branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($branch)) {
        throw 'Push refused: the repository is in detached-HEAD state.'
    }
    if ($branch -notmatch '^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$' -or
        $branch.Contains('..') -or $branch.Contains('@{') -or
        $branch.EndsWith('/') -or $branch.EndsWith('.')) {
        throw 'Push refused: current branch name is not safe for public metadata.'
    }
    & $checker -Scope Index -InputText $branch -InputLabel 'branch-name'
    $remoteUrl = & $gitCommand.Source -C $root remote get-url $Remote 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($remoteUrl -join "`n"))) {
        throw 'Push refused: configured Git remote could not be resolved.'
    }
    & $checker -Scope Index -InputText ($remoteUrl -join "`n") -InputLabel 'remote-url'
    Write-Host "[FlexShow] Pushing $branch to $Remote."
    Invoke-CheckedGit `
        -Arguments @('push', '--no-follow-tags', $Remote, "HEAD:refs/heads/$branch") `
        -DiscardOutput `
        -Operation 'Git push' | Out-Null
}

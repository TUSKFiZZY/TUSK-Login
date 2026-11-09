# Guild Board: sign and publish a release that GitHub built
#
# The second half of a release. The first half is the workflow .github\workflows\release.yml, which builds the installer
# from the tag in the open and attests it; it holds no secret and cannot publish. This script runs on the maintainer's PC
# after that workflow succeeded. It is the only place the release key is used, and the key never goes to GitHub.
#
# In order, and stopping with a plain sentence at any failure, with nothing published:
#   1. gh is logged in as TUSKFiZZY, GUILDBOARD_RELEASE_KEY names the key (outside this repository), and the key is the one
#      the app pins (selfupdate.py). Nothing is downloaded before this.
#   2. The tag has no release yet where it is to be released: the repository that built it, which installed copies of
#      2.0.1 and later look in, or with -Bridge the old QL1STY/guild-board-releases.
#   3. The workflow ran successfully for the tag, on the commit the tag names now.
#   4. Its installer and its SHA-256 are downloaded into a new folder outside this repository, and the SHA-256 is checked.
#   5. gh attestation verify checks that the installer was built by the workflow in the repository, unless -NoAttestation.
#   6. A draft release is made with the installer, which installed copies do not see, on the tag that is there already;
#      it is read back, signed with the release key, the signature is uploaded and read back, and only then is it
#      published (tools\release-signing.ps1).
#
# Run it from this folder's parent with, for example:
#     powershell -ExecutionPolicy Bypass -File tools\sign-release.ps1 -Tag v1.0.0
#
# -Repo is the repository that built the installer, TUSKFiZZY/TUSK-Login by default, and the release is made there.
# -NoAttestation skips step 5 for a trial on a private repository, where attestations may not be available; it is refused
# for a public one. The release notes are those of the version in src\guildboard\reference\changes.json at the tag.
#
# -Bridge releases the same installer, built and attested by -Repo, in QL1STY/guild-board-releases instead: the repository
# 2.0.0 and earlier look in. 2.0.1, the first version that looks in the code's repository, is released in both, with a run
# without -Bridge and one with it, so that it brings the older copies over. Each run stands alone and can be run again.

param(
    [Parameter(Mandatory = $true)][string]$Tag,
    [string]$Repo = 'TUSKFiZZY/TUSK-Login',
    [switch]$NoAttestation,
    [switch]$Bridge
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
Set-Location -LiteralPath $root
$account = 'TUSKFiZZY'
# Where it is released: the repository that built it, or the old releases repository with -Bridge.
$releases = if ($Bridge) { 'QL1STY/guild-board-releases' } else { $Repo }
$artifact = 'tuskfizzy-installer'
$workflow = 'release.yml'
. (Join-Path $PSScriptRoot 'release-signing.ps1')
# What gh writes (the release notes) is read as the UTF-8 it is.
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$unpublished = 'Nothing was published.'
if ($Tag -notmatch '^v[0-9]+\.[0-9]+\.[0-9]+$') { Stop-With "The tag is a v and three numbers, for example v1.0.0. $unpublished" }
if ($Repo -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
    Stop-With "The repository is written owner/name, for example $account/TUSK-Login. $unpublished"
}
$Version = $Tag.Substring(1)

# 1. Who releases, and with which key, before anything is downloaded.
try { Assert-GhAccount $account } catch { Stop-With "$($_.Exception.Message) $unpublished" }
try {
    $keyFile = Get-ReleaseKeyFile $root
    $trusted = Assert-ReleaseKeyTrusted $keyFile (Join-Path $root 'src\guildboard\selfupdate.py')
} catch {
    Stop-With "$($_.Exception.Message) $unpublished"
}

# 2. A tag is released once.
switch (Get-Draft $Tag $releases) {
    'false' { Stop-With "Version $Version is already released in $releases. $unpublished" }
    'true' {
        Stop-With ("$unpublished A draft of $Tag is left in $releases from a run that did not finish. Delete it with: " +
            "gh release delete $Tag --repo $releases --yes")
    }
}
if ($NoAttestation) {
    $private = (gh repo view $Repo --json isPrivate --jq .isPrivate)
    if ($LASTEXITCODE -ne 0) { Stop-With "GitHub did not say whether $Repo is private. $unpublished" }
    if ("$private".Trim() -ne 'true') {
        Stop-With "-NoAttestation is for a trial on a private repository, and $Repo is not private. $unpublished"
    }
    Write-Output "- Attestation skipped (-NoAttestation): this installer is not proven to come from $Repo's commit."
}

# 3. The workflow's successful run of this tag, built on the commit the tag names now.
Write-Output "- Find the run of $workflow for $Tag in $Repo"
$listed = (gh run list --repo $Repo --workflow $workflow --branch $Tag --status success --limit 20 --json 'databaseId,headSha,event') -join "`n"
if ($LASTEXITCODE -ne 0) { Stop-With "GitHub did not list the runs of $workflow in $Repo. $unpublished" }
$parsed = $listed | ConvertFrom-Json  # PowerShell 5.1 hands an array on as one object
$runs = @($parsed)
if ($runs.Count -eq 0) {
    Stop-With ("No successful run of $workflow was found for $Tag in $Repo. Push the tag, or run the workflow on it, " +
        "wait for it to succeed and run this again. $unpublished")
}
$run = $runs[0]
$tagged = "$(gh api "repos/$Repo/commits/$Tag" --jq .sha)".Trim()
if ($LASTEXITCODE -ne 0 -or $tagged -notmatch '^[0-9a-f]{40}$') {
    Stop-With "GitHub did not say which commit $Tag names in $Repo. $unpublished"
}
$built = "$($run.headSha)"
if ($tagged -cne $built) {
    Stop-With ("The tag $Tag now names commit $($tagged.Substring(0, 7)), but the run built " +
        "$($built.Substring(0, [Math]::Min(7, $built.Length))). The tag was moved after the build; build it again. $unpublished")
}

# 4. What it built, into a folder made for this alone, outside this repository.
$folder = Join-Path ([IO.Path]::GetTempPath()) ("tuskfizzy-release-$Tag-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
if (($folder + '\').StartsWith("$root\", [StringComparison]::OrdinalIgnoreCase) -or (Test-Path -LiteralPath $folder)) {
    Stop-With "The folder for the download is inside this repository or already there. $unpublished"
}
Write-Output "- Download the installer of run $($run.databaseId)"
gh run download "$($run.databaseId)" --repo $Repo --name $artifact --dir $folder
if ($LASTEXITCODE -ne 0) { Stop-With "The installer could not be downloaded from run $($run.databaseId). $unpublished" }
$setup = Join-Path $folder "TUSKFiZZY Setup $Version.exe"
$sumFile = "$setup.sha256"
if (-not (Test-Path -LiteralPath $setup -PathType Leaf) -or -not (Test-Path -LiteralPath $sumFile -PathType Leaf)) {
    Stop-With ("The run's artifact does not hold 'TUSKFiZZY Setup $Version.exe' and its .sha256 file, so $Tag is not " +
        "the version it built. $unpublished")
}
$expected = ([IO.File]::ReadAllText($sumFile).Trim() -split '\s+')[0]
$actual = (Get-FileSha256 $setup)
if ($expected -cnotmatch '^[0-9a-f]{64}$' -or $expected -cne $actual) {
    Stop-With "The installer's SHA-256 is $actual, not the one the build wrote down. It was changed after the build. $unpublished"
}
Write-Output "  SHA-256 $actual"

# 5. That GitHub built it from this repository's workflow.
if (-not $NoAttestation) {
    Write-Output "- Check the attestation against $Repo"
    gh attestation verify $setup --repo $Repo
    if ($LASTEXITCODE -ne 0) {
        Stop-With "gh could not verify that $Repo built this installer, so it is not signed. $unpublished"
    }
}

# 6. The release notes of the tagged commit, the draft, the signature, and the publication.
Write-Output "- Read the release notes of $Tag"
$changes = (gh api -H 'Accept: application/vnd.github.raw+json' "repos/$Repo/contents/src/guildboard/reference/changes.json?ref=$Tag") -join "`n"
if ($LASTEXITCODE -ne 0) { Stop-With "GitHub did not give changes.json at $Tag. $unpublished" }
try { $notes = Get-ReleaseNotes $Version $changes } catch { Stop-With "changes.json at $Tag is not readable. $unpublished" }

Write-Output "- Upload TUSKFiZZY $Version to $releases as a draft, which installed copies do not see"
# In the repository that built it, the tag is there already, and the release must be on it.
if (-not (New-DraftRelease $Tag $Version $setup $notes $releases -VerifyTag:(-not $Bridge))) {
    Stop-With "The draft could not be made. $unpublished"
}
Publish-SignedDraft $Tag $Version $setup $keyFile $trusted $releases

Remove-Item -LiteralPath $folder -Recurse -Force -ErrorAction SilentlyContinue
Write-Output "TUSKFiZZY $Version is released in $releases. Installed copies that look there offer it within twelve hours, or at their next start."
Write-Output "It was built by $Repo at commit $($tagged.Substring(0, 7)); the installer's SHA-256 is $actual."

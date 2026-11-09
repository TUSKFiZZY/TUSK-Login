# Guild Board release signatures
Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop
#
# What signs a release for the installed copies, which offer an update only with its signature (selfupdate.py):
# ECDSA on the curve P-256 over the SHA-256 of the text "<tag>`n<installer name>`n<size>`n<sha256>", the release's tag
# and its installer's name, size in bytes and SHA-256 in lower-case hex as GitHub's API gives them, with no line's end
# after the last. The signature is written as IEEE P1363, r then s, 32 bytes each, to "<installer name>.sig", which
# is uploaded beside the installer. The key's private half is a PKCS#8 file made once with tools\make-release-key.ps1,
# kept outside this repository; nothing here writes it anywhere or prints it.
#
# release.ps1, tools\sign-release.ps1, build-installer.ps1 and make-release-key.ps1 load these functions with:
#     . (Join-Path $PSScriptRoot 'tools\release-signing.ps1')
# They use .NET's own ECDsaCng, as Windows PowerShell 5.1 and PowerShell 7 both have it. Below the signature are the
# steps that release.ps1 and sign-release.ps1 share: who is logged in to GitHub, where the key is, the draft as GitHub
# shows it, the release notes, and signing and publishing the draft.

function New-ReleaseKey([string]$Path) {
    # A new key: its private half written to $Path as PKCS#8 PEM, a file that must not exist yet. Gives its public half,
    # as the app pins it.
    $parameters = New-Object Security.Cryptography.CngKeyCreationParameters
    $parameters.ExportPolicy = [Security.Cryptography.CngExportPolicies]::AllowPlaintextExport
    # A key with no name is kept in memory alone. PowerShell hands $null to a .NET text as "", which would name a key
    # kept in Windows' store for this user, so the name is a real null.
    $key = [Security.Cryptography.CngKey]::Create(
        [Security.Cryptography.CngAlgorithm]::ECDsaP256, [NullString]::Value, $parameters
    )
    try {
        if (-not $key.IsEphemeral) {
            $key.Delete()
            throw 'Windows kept the new key in its store, so it was removed and no key was made.'
        }
        $der = $key.Export([Security.Cryptography.CngKeyBlobFormat]::Pkcs8PrivateBlob)
        $lines = [Convert]::ToBase64String($der) -split '(.{64})' | Where-Object { $_ }
        $pem = [Text.Encoding]::ASCII.GetBytes(
            "-----BEGIN PRIVATE KEY-----`n" + ($lines -join "`n") + "`n-----END PRIVATE KEY-----`n"
        )
        # CreateNew: an existing file is never replaced, and the new one is opened for no one else while it is written.
        $file = New-Object IO.FileStream($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $file.Write($pem, 0, $pem.Length) } finally { $file.Dispose() }
        [Array]::Clear($der, 0, $der.Length)
        [Array]::Clear($pem, 0, $pem.Length)
        return Get-PublicPoint $key
    } finally {
        $key.Dispose()
    }
}

function Read-ReleaseKey([string]$Path) {
    # The private key from its PKCS#8 PEM file, for ECDSA on P-256. The caller disposes of it.
    $text = [IO.File]::ReadAllText($Path)
    $der = [Convert]::FromBase64String((($text -replace '-----(BEGIN|END) PRIVATE KEY-----', '') -replace '\s', ''))
    try {
        $key = [Security.Cryptography.CngKey]::Import($der, [Security.Cryptography.CngKeyBlobFormat]::Pkcs8PrivateBlob)
    } finally {
        [Array]::Clear($der, 0, $der.Length)
    }
    if ("$($key.Algorithm)" -ne 'ECDSA_P256') {
        $key.Dispose()
        throw "The key in $Path is not one for ECDSA on P-256."
    }
    return $key
}

function Get-PublicPoint([Security.Cryptography.CngKey]$Key) {
    # The key's public half as the app pins it (RELEASE_KEY in selfupdate.py): the uncompressed point, 04 then x and y,
    # in lower-case hex. CNG's blob holds the magic and the length of a coordinate first.
    $blob = $Key.Export([Security.Cryptography.CngKeyBlobFormat]::EccPublicBlob)
    return '04' + (($blob[8..($blob.Length - 1)] | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Get-TrustedKey([string]$Source) {
    # The key the app pins: RELEASE_KEY in selfupdate.py, empty while none was made.
    $found = Select-String -LiteralPath $Source -Pattern '^RELEASE_KEY = "([0-9a-f]*)"' | Select-Object -First 1
    if ($null -eq $found) { return '' }
    return $found.Matches[0].Groups[1].Value
}

function Test-TrustedKey([string]$Key) {
    # Whether a pinned key is one the app can check with: P-256's uncompressed point, 04 then x and y, in lower-case hex.
    return $Key -cmatch '^04[0-9a-f]{128}\z'
}

function Test-AssetUrl([string]$Url) {
    # Whether an asset's address is one installed copies download from (INSTALLER_ASSET in netpolicy.py): the code's
    # repository's, where 2.0.1 and later look, or the old releases repository's, where 2.0.0 and earlier look and which
    # the bridge release 2.0.1 reached them in.
    return $Url -cmatch '^https://api\.github\.com/repos/(?:TUSKFiZZY/TUSK-Login|QL1STY/guild-board-releases)/releases/assets/[0-9]+\z'
}

function Get-ReleaseText([string]$Tag, [string]$Name, [long]$Size, [string]$Sha256) {
    # What the key signs, as the app builds it (release_text in selfupdate.py).
    return "$Tag`n$Name`n$Size`n$Sha256"
}

function New-ReleaseSignature([Security.Cryptography.CngKey]$Key, [string]$Text) {
    $ecdsa = [Security.Cryptography.ECDsaCng]::new($Key)
    try {
        $data = [Text.Encoding]::UTF8.GetBytes($Text)
        # The comma keeps the bytes together: PowerShell would hand them on one by one.
        return , $ecdsa.SignData($data, [Security.Cryptography.HashAlgorithmName]::SHA256)
    } finally {
        $ecdsa.Dispose()
    }
}

function Test-ReleaseSignature([string]$Trusted, [string]$Text, [byte[]]$Signature) {
    # Whether the signature is good for the text under the key the app pins: what an installed copy will find.
    if (-not (Test-TrustedKey $Trusted) -or $Signature.Length -ne 64) { return $false }
    # CNG's blob for the key: the magic of a P-256 public key for ECDSA ("ECS1"), the length of a coordinate, x and y.
    $blob = New-Object byte[] 72
    [BitConverter]::GetBytes([uint32]0x31534345).CopyTo($blob, 0)
    [BitConverter]::GetBytes([uint32]32).CopyTo($blob, 4)
    for ($i = 0; $i -lt 64; $i++) { $blob[8 + $i] = [Convert]::ToByte($Trusted.Substring(2 + 2 * $i, 2), 16) }
    $public = [Security.Cryptography.CngKey]::Import($blob, [Security.Cryptography.CngKeyBlobFormat]::EccPublicBlob)
    $ecdsa = [Security.Cryptography.ECDsaCng]::new($public)
    try {
        $data = [Text.Encoding]::UTF8.GetBytes($Text)
        return $ecdsa.VerifyData($data, $Signature, [Security.Cryptography.HashAlgorithmName]::SHA256)
    } finally {
        $ecdsa.Dispose()
        $public.Dispose()
    }
}

function Get-ReleaseFacts([string]$Json, [string]$Installer) {
    # The installer as GitHub's API answer for a release shows it, and the text to sign for it. The installer is the
    # asset whose name ends in .exe, as the app takes it; the app takes it only from the releases repository's assets and
    # at most 600000000 bytes (MAX_INSTALLER_BYTES in selfupdate.py); and its size and checksum, in the same letters,
    # must be the file's that was uploaded.
    $release = $Json | ConvertFrom-Json
    $found = @($release.assets | Where-Object { "$($_.name)".ToLowerInvariant().EndsWith('.exe') })
    if ($found.Count -eq 0) { throw 'The release has no installer.' }
    if ($found.Count -gt 1) { throw 'The release has more than one installer, and installed copies take the first.' }
    $asset = $found[0]
    $name = "$($asset.name)"
    if ($name -match '[\\/:*?"<>|]') { throw "GitHub named the installer '$name', which is no file name." }
    if (-not (Test-AssetUrl "$($asset.url)")) {
        throw "GitHub gives the installer an address installed copies do not download from: $($asset.url)"
    }
    if ([long]$asset.size -le 0 -or [long]$asset.size -gt 600000000) {
        throw "GitHub's installer is $($asset.size) bytes, and installed copies take at most 600000000."
    }
    $sha256 = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant()
    $size = (Get-Item -LiteralPath $Installer).Length
    if ([long]$asset.size -ne $size -or "$($asset.digest)" -cne "sha256:$sha256") {
        throw ("GitHub's installer ($($asset.size) bytes, $($asset.digest)) is not the one that was uploaded " +
            "($size bytes, sha256:$sha256).")
    }
    return [pscustomobject]@{ Name = $name; Text = (Get-ReleaseText "$($release.tag_name)" $name $size $sha256) }
}

function New-ReleaseSignatureFile([string]$Json, [string]$Installer, [string]$KeyFile, [string]$Trusted,
    [string]$Folder) {
    # Signs the release GitHub's API answer describes, checks the signature with the key the app pins, and writes it to
    # "<installer name>.sig" in $Folder. Gives the file's path.
    $facts = Get-ReleaseFacts $Json $Installer
    $key = Read-ReleaseKey $KeyFile
    try { $signature = New-ReleaseSignature $key $facts.Text } finally { $key.Dispose() }
    if (-not (Test-ReleaseSignature $Trusted $facts.Text $signature)) {
        throw ('The signature does not check with the key the app trusts. RELEASE_KEY in src\guildboard\selfupdate.py ' +
            'must be the line tools\make-release-key.ps1 printed for this key.')
    }
    $path = Join-Path $Folder "$($facts.Name).sig"
    [IO.File]::WriteAllBytes($path, $signature)
    return $path
}

function Test-SignedRelease([string]$Json, [string]$Installer, [string]$Signature, [string]$Trusted) {
    # Whether GitHub's answer for the release shows it as installed copies will read it: the installer that was
    # uploaded, and beside it, under its name with ".sig" after, at one of the repository's asset addresses, 64 bytes
    # with GitHub's checksum in the same letters, this signature, which checks with the pinned key.
    $facts = Get-ReleaseFacts $Json $Installer
    $bytes = [IO.File]::ReadAllBytes($Signature)
    $release = $Json | ConvertFrom-Json
    $shown = @($release.assets | Where-Object { "$($_.name)" -ceq "$($facts.Name).sig" })
    if ($shown.Count -ne 1) { return $false }
    $asset = $shown[0]
    $sha256 = (Get-FileHash -LiteralPath $Signature -Algorithm SHA256).Hash.ToLowerInvariant()
    if (-not (Test-AssetUrl "$($asset.url)") -or [long]$asset.size -ne 64 -or [long]$asset.size -ne $bytes.Length) {
        return $false
    }
    if ("$($asset.digest)" -cne "sha256:$sha256") { return $false }
    return Test-ReleaseSignature $Trusted $facts.Text $bytes
}

# ---------------------------------------------------------------- the steps both release scripts take

function Stop-With([string]$Text) {
    # Says why the release stops, and stops the script that loaded this file.
    Write-Output $Text
    exit 1
}

function Test-Gh([scriptblock]$Ask) {
    # Whether gh answers yes. Its "not found" goes to the error stream, which would stop a script.
    $ErrorActionPreference = 'Continue'
    & $Ask *> $null
    return $LASTEXITCODE -eq 0
}

function Assert-GhAccount([string]$Account) {
    # Stops unless gh is installed and logged in as $Account, the only account that releases.
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'The GitHub CLI, gh, is not installed.' }
    $ErrorActionPreference = 'Continue'
    $login = (gh api user --jq .login)
    if ($LASTEXITCODE -ne 0 -or "$login".Trim() -ne $Account) {
        throw "gh is logged in as '$login', not $Account. Switch with: gh auth switch --user $Account"
    }
}

function Get-ReleaseKeyFile([string]$Root) {
    # The release key's file, which GUILDBOARD_RELEASE_KEY names: it must exist, and be outside $Root, the repository,
    # where it could be committed. Gives its full path.
    $keyFile = $env:GUILDBOARD_RELEASE_KEY
    if (-not $keyFile -or -not (Test-Path -LiteralPath $keyFile -PathType Leaf)) {
        throw ('GUILDBOARD_RELEASE_KEY does not name the release key''s file. Make the key once with ' +
            'tools\make-release-key.ps1, or set the variable to where its file is, and open a new window.')
    }
    $keyFile = (Resolve-Path -LiteralPath $keyFile).ProviderPath
    if ($keyFile.StartsWith("$Root\", [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The release key is inside this folder, where it could be committed. Move it out and set GUILDBOARD_RELEASE_KEY again.'
    }
    return $keyFile
}

function Assert-ReleaseKeyTrusted([string]$KeyFile, [string]$Source) {
    # Stops unless the key in $KeyFile is the one the app pins in $Source (selfupdate.py). Gives the pinned key.
    $trusted = Get-TrustedKey $Source
    $key = Read-ReleaseKey $KeyFile
    try { $point = Get-PublicPoint $key } finally { $key.Dispose() }
    if ($point -cne $trusted) {
        throw ('The release key is not the one the app trusts. RELEASE_KEY in src\guildboard\selfupdate.py and in ' +
            'tests\test_handsoff.py must be the line tools\make-release-key.ps1 printed for this key.')
    }
    return $trusted
}

function Get-Draft([string]$Tag, [string]$Repo) {
    # 'true' when the release of this tag in $Repo is a draft, 'false' when it is published, and nothing when there is none.
    $ErrorActionPreference = 'Continue'
    $answer = gh release view $Tag --repo $Repo --json isDraft --jq .isDraft 2> $null
    if ($LASTEXITCODE -ne 0) { return $null }
    return "$answer".Trim()
}

function Read-Draft([string]$Tag, [string]$Repo) {
    # GitHub's API answer for the draft of this tag, as text: the facts installed copies will read once it is published.
    $listed = (gh api "repos/$Repo/releases?per_page=30") -join "`n"
    if ($LASTEXITCODE -ne 0) { return $null }
    $releases = $listed | ConvertFrom-Json
    $draft = @($releases | Where-Object { $_.draft -and $_.tag_name -eq $Tag }) | Select-Object -First 1
    if ($null -eq $draft) { return $null }
    $answer = (gh api "repos/$Repo/releases/$($draft.id)") -join "`n"
    if ($LASTEXITCODE -ne 0) { return $null }
    return $answer
}

function Get-ReleaseNotes([string]$Version, [string]$ChangesJson) {
    # The notes of this version in changes.json (its text), one bullet per item: "- [New] Title: text" (or Better,
    # Fixed), and "- Title" for a plain line without a kind. Installed copies read these bullets back (selfupdate.py,
    # parse_notes). Without an entry, the plain line.
    $changes = $ChangesJson | ConvertFrom-Json
    $lines = $changes.PSObject.Properties | Where-Object { $_.Name -eq $Version } | ForEach-Object { $_.Value }
    if (-not $lines) { return "TUSKFiZZY $Version." }
    $bullets = $lines | ForEach-Object {
        if ($_ -is [string]) { "- $_" }
        else {
            $kind = (Get-Culture).TextInfo.ToTitleCase($_.kind)
            if ($_.text) { "- [$kind] $($_.title): $($_.text)" } else { "- [$kind] $($_.title)" }
        }
    }
    return $bullets -join "`n"
}

function New-DraftRelease([string]$Tag, [string]$Version, [string]$Installer, [string]$Notes, [string]$Repo,
    [switch]$VerifyTag) {
    # Uploads the installer to a draft release of $Repo, which installed copies do not see. The notes go through a file:
    # PowerShell 5.1 splits an argument that holds double quotes into pieces, and gh would take the pieces for files to
    # attach. The file has no byte order mark, which the notes' reader would not skip. With -VerifyTag the tag must be
    # in $Repo already, as in the repository that built the installer: GitHub would otherwise make it on its main. True
    # when the draft was made.
    $notesFile = Join-Path ([IO.Path]::GetTempPath()) "guildboard-notes-$Version.md"
    [IO.File]::WriteAllText($notesFile, $Notes, (New-Object Text.UTF8Encoding $false))
    $verify = @(if ($VerifyTag) { '--verify-tag' })
    gh release create $Tag $Installer --repo $Repo --title "TUSKFiZZY $Version" --notes-file $notesFile --draft @verify | Out-Host
    $drafted = $LASTEXITCODE -eq 0
    Remove-Item -LiteralPath $notesFile -ErrorAction SilentlyContinue
    return $drafted
}

function Publish-SignedDraft([string]$Tag, [string]$Version, [string]$Installer, [string]$KeyFile, [string]$Trusted,
    [string]$Repo) {
    # From a draft with the installer in it: signs the installer as GitHub shows it, uploads the signature, reads the
    # draft back and publishes it only when it shows the signature beside the installer as installed copies look for it.
    # Stops the script, with the draft left unpublished, at any step that does not work.
    Write-Output "- Sign the installer with the release key"
    $unpublished = "The draft of $Tag stays on GitHub, unpublished; delete it with: gh release delete $Tag --repo $Repo --yes"
    $json = Read-Draft $Tag $Repo
    if ($null -eq $json) { Stop-With "GitHub did not show the draft. $unpublished" }
    $folder = Join-Path ([IO.Path]::GetTempPath()) "guildboard-signature-$Version"
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    try {
        $signature = New-ReleaseSignatureFile -Json $json -Installer $Installer -KeyFile $KeyFile -Trusted $Trusted -Folder $folder
    } catch {
        Stop-With "$($_.Exception.Message) $unpublished"
    }
    gh release upload $Tag $signature --repo $Repo
    if ($LASTEXITCODE -ne 0) { Stop-With "The signature could not be uploaded. $unpublished" }
    $json = Read-Draft $Tag $Repo
    if ($null -eq $json -or -not (Test-SignedRelease $json $Installer $signature $Trusted)) {
        Stop-With "GitHub does not show the signature beside the installer as installed copies look for it. $unpublished"
    }
    Remove-Item -LiteralPath $folder -Recurse -Force -ErrorAction SilentlyContinue

    Write-Output "- Publish TUSKFiZZY $Version"
    gh release edit $Tag --repo $Repo --draft=false --latest
    if ($LASTEXITCODE -ne 0) { Stop-With "The release could not be published. It is still a draft on GitHub; publish it there." }
}

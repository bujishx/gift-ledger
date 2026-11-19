# Windows PowerShell 5.1. Binary stdin/stdout are local pipes to the owner tool.
param([ValidateSet('read','create','select-key','select-new','select-backup')][string]$Action)
$ErrorActionPreference = 'Stop'
try {
    if ($Action.StartsWith('select-')) {
        Add-Type -AssemblyName System.Windows.Forms
        if ($Action -eq 'select-new') {
            $dialog = New-Object System.Windows.Forms.SaveFileDialog
            $dialog.FileName = 'GiftLedger.agekey'
            $dialog.Filter = 'age key|*.agekey|All files|*.*'
            $dialog.OverwritePrompt = $false  # The client requires an unused path.
        } else {
            $dialog = New-Object System.Windows.Forms.OpenFileDialog
            if ($Action -eq 'select-backup') {
                $dialog.Filter = 'GiftLedger backup (ZIP or BUNDLE.json)|*.zip;BUNDLE.json'
            } else {
                $dialog.Filter = 'age key|*.agekey;*.txt|All files|*.*'
            }
        }
        # The PowerShell helper has no visible main window. Give the native
        # dialog its own foreground owner while the console is waiting.
        $owner = New-Object System.Windows.Forms.Form
        $owner.ShowInTaskbar = $false
        $owner.StartPosition = 'CenterScreen'
        $owner.Opacity = 0
        $owner.TopMost = $true
        try {
            $owner.Show()
            $owner.Activate()
            if ($dialog.ShowDialog($owner) -ne [System.Windows.Forms.DialogResult]::OK) { exit 2 }
            [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
            [Console]::Write($dialog.FileName)
        } finally {
            $dialog.Dispose()
            $owner.Dispose()
        }
        exit 0
    }

    $path = [IO.Path]::GetFullPath($env:GIFTLEDGER_SELECTED_KEY)
    if ($path -notmatch '^[A-Za-z]:\\' -or $path.Substring(2).Contains(':')) { throw 'Local file required' }
    $drive = New-Object IO.DriveInfo([IO.Path]::GetPathRoot($path))
    if ($drive.DriveType -notin @('Fixed','Removable')) { throw 'Local drive required' }
    for ($parent = [IO.Directory]::GetParent($path); $null -ne $parent; $parent = $parent.Parent) {
        if (($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Reparse point' }
    }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $allowed = @($sid.Value, 'S-1-5-18', 'S-1-5-32-544')
    if ($Action -eq 'create') {
        # CreateNew installs the protected DACL before the first secret byte is written.
        $acl = New-Object Security.AccessControl.FileSecurity
        $acl.SetOwner($sid)
        $acl.SetAccessRuleProtection($true, $false)
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','Allow'))
        $rights = [Security.AccessControl.FileSystemRights]::FullControl
        $stream = [IO.FileStream]::new($path, 'CreateNew', $rights, 'None', 4096, 'None', $acl)
    } else {
        $stream = [IO.File]::Open($path, 'Open', 'Read', 'None')
    }
    try {
        if (([IO.File]::GetAttributes($path) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Reparse point' }
        $acl = $stream.GetAccessControl()
        if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $allowed) { throw 'Owner mismatch' }
        $raw = [Security.AccessControl.RawSecurityDescriptor]::new($acl.GetSecurityDescriptorBinaryForm(), 0)
        if ($null -eq $raw.DiscretionaryAcl -or $raw.DiscretionaryAcl.Count -eq 0) { throw 'Private DACL required' }
        foreach ($ace in $raw.DiscretionaryAcl) {
            if ($ace.AceType -ne 'AccessDenied' -and
                ($ace.AceType -ne 'AccessAllowed' -or $ace.SecurityIdentifier.Value -notin $allowed)) {
                throw 'Access granted to another principal'
            }
        }
        if ($Action -eq 'create') {
            [Console]::OpenStandardInput().CopyTo($stream)
            $stream.Flush($true)
        } else {
            if ($stream.Length -gt 16384) { throw 'Key too large' }
            $stream.CopyTo([Console]::OpenStandardOutput())
        }
    } finally { $stream.Dispose() }
} catch {
    [Console]::Error.WriteLine('GiftLedger could not open the selected private local file or dialog.')
    exit 1
}

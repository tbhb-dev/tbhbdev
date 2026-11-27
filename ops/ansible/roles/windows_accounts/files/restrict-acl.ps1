# Leaves a file's ACL as full control for exactly the named accounts and
# nothing inherited, which Windows OpenSSH requires of an authorized_keys
# file. It reports a change only when the ACL differs, and honors check mode.
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)][string[]]$Accounts
)
$Ansible.Changed = $false
$sidType = [Security.Principal.SecurityIdentifier]
$want = $Accounts | ForEach-Object {
    if ($_ -match '^S-1-') { $_ } else { ([Security.Principal.NTAccount]$_).Translate($sidType).Value }
} | Sort-Object -Unique
$acl = Get-Acl -LiteralPath $Path
$rules = @($acl.Access)
$have = $rules | ForEach-Object { $_.IdentityReference.Translate($sidType).Value } | Sort-Object -Unique
$plain = @($rules | Where-Object {
    $_.AccessControlType -ne 'Allow' -or $_.FileSystemRights -ne 'FullControl'
}).Count -eq 0
if (-not ($acl.AreAccessRulesProtected -and $plain -and ($want -join ',') -eq ($have -join ','))) {
    if ($PSCmdlet.ShouldProcess($Path, 'Restrict the ACL')) {
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRule($rule) }
        foreach ($sid in $want) {
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
                [Security.Principal.SecurityIdentifier]$sid, 'FullControl', 'Allow'))
        }
        Set-Acl -LiteralPath $Path -AclObject $acl
    }
    $Ansible.Changed = $true
}

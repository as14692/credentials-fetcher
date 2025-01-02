
# This script does the following:
# 1) Install/Update SSM agent - without this the domain-join can fail
# 2) Create a new OU
# 3) Create a new security group
# 4) Create a new standard user account, this account's username and password needs to be stored in a secret store like AWS secrets manager.
# 5) Add members to the security group that is allowed to retrieve gMSA password
# 6) Create gMSA accounts with PrincipalsAllowedToRetrievePassword set to the security group created in 4)

# 1) Install SSM agent
Write-Output "Updating SSM agent..."
[System.Net.ServicePointManager]::SecurityProtocol = 'TLS12'
$progressPreference = 'silentlyContinue'
Invoke-WebRequest https://s3.amazonaws.com/ec2-downloads-windows/SSMAgent/latest/windows_amd64/AmazonSSMAgentSetup.exe   -OutFile $env:USERPROFILE\Desktop\SSMAgent_latest.exe
Start-Process -FilePath $env:USERPROFILE\Desktop\SSMAgent_latest.exe  -ArgumentList "/S"

# To install the AD module on Windows Server, run Install-WindowsFeature RSAT-AD-PowerShell
# To install the AD module on Windows 10 version 1809 or later, run Add-WindowsCapability -Online -Name 'Rsat.ActiveDirectory.DS-LDS.Tools~~~~0.0.1.0'
# To install the AD module on older versions of Windows 10, see https://aka.ms/rsat
Write-Output "Installing Active Directory management tools..."
Install-WindowsFeature -Name "RSAT-AD-Tools" -IncludeAllSubFeature
Install-WindowsFeature RSAT-AD-PowerShell
Install-Module CredentialSpec
Install-Module -Name SqlServer -AllowClobber -Force

$username = "admin@CONTOSO.COM"
$password = "Qn:51eJsORJNL@~{HY@?" | ConvertTo-SecureString -AsPlainText -Force
$credential = New-Object System.Management.Automation.PSCredential($username, $password)
$groupAllowedToRetrievePassword = "WebAppAccounts_OU"
# This is the basedn path that needs to be in secrets manager as "distinguishedName" :  "OU=MYOU,OU=Users,OU=ActiveDirectory,DC=contoso,DC=com"
$path = "OU=MYOU,OU=Users,OU=contoso,DC=contoso,DC=com"


# 2) Create OU
New-ADOrganizationalUnit -Name "MYOU" -Path "OU=Users,OU=contoso,DC=contoso,DC=com" -Credential $credential

# 3) Create the security group
try {
  New-ADGroup -Name "WebApp Authorized Accounts in OU" -SamAccountName $groupAllowedToRetrievePassword -Credential $credential -GroupScope DomainLocal  -Server contoso.com
} catch {
  Write-Output "Security Group created"
}

# 4) Create a new standard user account, this account's username and password needs to be stored in a secret store like AWS secrets manager.
try {
  New-ADUser -Name "StandardUser01" -AccountPassword (ConvertTo-SecureString -AsPlainText "p@ssw0rd" -Force) -Enabled 1 -Credential $credential -Path $path -Server contoso.com
} catch {
  Write-Output "Created StandardUser01"
}

# 5) Add members to the security group that is allowed to retrieve gMSA password
try {
  Add-ADGroupMember -Identity $groupAllowedToRetrievePassword -Members "StandardUser01" -Credential $credential -Server contoso.com
  Add-ADGroupMember -Identity $groupAllowedToRetrievePassword -Members "admin" -Credential $credential -Server contoso.com
} catch {
  Write-Output "Created AD Group $groupAllowedToRetrievePassword"
}

# 6) Create gMSA accounts with PrincipalsAllowedToRetrievePassword set to the security group created in 4)
$string_err = ""
for (($i = 1); $i -le 10; $i++)
{
    # Create the gMSA account
    $gmsa_account_name = "WebApp0" + $i
    $gmsa_account_with_domain = $gmsa_account_name + ".contoso.com"
    $gmsa_account_with_host = "host/" + $gmsa_account_name
    $gmsa_account_with_host_and_domain = $gmsa_account_with_host + ".contoso.com"

    try {
       #New-ADServiceAccount -Name serviceuser1 -Path "OU=MYOU1,OU=Users,OU=ActiveDirectory,DC=contoso,DC=com" -Credential $credential -DNSHostname "contoso.com"
        New-ADServiceAccount -Name $gmsa_account_name -DnsHostName $gmsa_account_with_domain -ServicePrincipalNames $gmsa_account_with_host, $gmsa_account_with_host_and_domain -PrincipalsAllowedToRetrieveManagedPassword $groupAllowedToRetrievePassword -Path $path -Credential $credential -Server contoso.com
        Write-Output "New-ADServiceAccount -Name $gmsa_account_name -DnsHostName $gmsa_account_with_domain -ServicePrincipalNames $gmsa_account_with_host, $gmsa_account_with_host_and_domain -PrincipalsAllowedToRetrieveManagedPassword $groupAllowedToRetrievePassword -Path $path -Credential $credential -Server contoso.com"
    } catch {
        $string_err = $_ | Out-String
        Write-Output "Error while gMSA account creation and copy credspec to S3 bucket: " + $string_err
    }
}

# Set the SQL Server instance name
$sqlInstance = $env:computername

New-NetFirewallRule -DisplayName "SQLServer default instance" -Direction Inbound -LocalPort 1433 -Protocol TCP -Action Allow
New-NetFirewallRule -DisplayName "SQLServer Browser service" -Direction Inbound -LocalPort 1434 -Protocol UDP -Action Allow
netsh advfirewall firewall add rule name = SQLPort dir = in protocol = tcp action = allow localport = 1433 remoteip = localsubnet profile = DOMAIN
New-NetFirewallRule -DisplayName “AllowRDP” -Direction Inbound -Protocol TCP –LocalPort 3389 -Action Allow
New-NetFirewallRule -DisplayName "AllowSQLServer" -Direction Inbound -Protocol TCP -LocalPort 1433 -Action Allow


# Create a connection string
$connectionString0 = "Server=$sqlInstance;Integrated Security=True;"
$connectionString1 = "Server=$sqlInstance;Database=EmployeesDB;Integrated Security=True;"

$createDatabaseQuery = "CREATE DATABASE EmployeesDB"

$query = @"
CREATE TABLE dbo.EmployeesTable (
    EmpID INT IDENTITY(1,1) PRIMARY KEY,
    EmpName VARCHAR(50) NOT NULL,
    Designation VARCHAR(50) NOT NULL,
    Department VARCHAR(50) NOT NULL,
    JoiningDate DATETIME NOT NULL
);

INSERT INTO EmployeesDB.dbo.EmployeesTable (EmpName, Designation, Department, JoiningDate)
VALUES
    ('CHIN YEN', 'LAB ASSISTANT', 'LAB', '2022-03-05 03:57:09.967'),
    ('MIKE PEARL', 'SENIOR ACCOUNTANT', 'ACCOUNTS', '2022-03-05 03:57:09.967'),
    ('GREEN FIELD', 'ACCOUNTANT', 'ACCOUNTS', '2022-03-05 03:57:09.967'),
    ('DEWANE PAUL', 'PROGRAMMER', 'IT', '2022-03-05 03:57:09.967'),
    ('MATTS', 'SR. PROGRAMMER', 'IT', '2022-03-05 03:57:09.967'),
    ('PLANK OTO', 'ACCOUNTANT', 'ACCOUNTS', '2022-03-05 03:57:09.967');
alter authorization on database::[EmployeesDB] to [WebApp01$]
"@


$createLoginQuery = "CREATE LOGIN [contoso\webapp01$] FROM WINDOWS WITH DEFAULT_DATABASE = [master], DEFAULT_LANGUAGE = [us_english];"
Invoke-Sqlcmd -ConnectionString $connectionString0 -Query $createLoginQuery

Invoke-Sqlcmd -ConnectionString $connectionString0 -Query $createDatabaseQuery -QueryTimeout 60
Invoke-Sqlcmd -ConnectionString $connectionString1 -Query $query
 
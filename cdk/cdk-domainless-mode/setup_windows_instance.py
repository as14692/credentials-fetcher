import boto3
import os
import json

"""
This script sets up the EC2 Windows instance.

This script performs the following operations:

1. Retrieves an EC2 instance ID based on a tag name.
2. Reads a PowerShell script ('gmsa.ps1') from the directory.
3. Executes the PowerShell script on the specified EC2 instance using AWS Systems Manager (SSM).
4. The Powershell script does the following:
    - Installs AD management tools and related PowerShell modules.
    - Creates a new Organizational Unit (OU) in Active Directory.
    - Creates a new security group for gMSA account management.
    - Creates a new standard user account.
    - Adds members to the security group for gMSA password retrieval.
    - Creates multiple Group Managed Service Accounts (gMSA) based on a specified count.
    - Configures SQL Server firewall rules:
    - Allows inbound traffic on ports 1433 (TCP) and 1434 (UDP).
    - Sets up rules for RDP and SQL Server access.
    - Creates a new SQL Server database named 'EmployeesDB'.
    - Creates a table 'EmployeesTable' and inserts sample data.
    - Alters the database authorization to allow access from a gMSA account.

It's designed to automate the process of running a PowerShell script on a Windows EC2 instance, 
for configuring Group Managed Service Accounts (gMSA) and related AWS resources.

"""

with open('data.json', 'r') as file:
    data = json.load(file)

def get_value(key):
    return os.environ.get(key, data.get(key.lower()))

def run_powershell_script(instance_id, script_path):

    with open(script_path, 'r') as file:
        script_content = file.read()

    script_content = script_content.replace("INPUTPASSWORD", data["domain_admin_password"])
    script_content = script_content.replace("DOMAINNAME", data["directory_name"])
    script_content = script_content.replace("NETBIOS_NAME", data["netbios_name"])
    script_content = script_content.replace("NUMBER_OF_GMSA_ACCOUNTS", str(data["number_of_gmsa_accounts"]))
    s3_bucket = get_value("S3_PREFIX") + data["s3_bucket_suffix"]
    script_content = script_content.replace("BUCKET_NAME", s3_bucket)
    
    ssm = boto3.client('ssm')

    response = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunPowerShellScript",
        Parameters={'commands': [script_content]}
    )

    command_id = response['Command']['CommandId']

    waiter = ssm.get_waiter('command_executed')
    try:
        waiter.wait(
            CommandId=command_id,
            InstanceId=instance_id,
            WaiterConfig={
                'Delay': 30,
                'MaxAttempts': 50
            }
        )
    except Exception as e:
        print(f"Command failed: {script_content}")
        print(f"Error: {str(e)}")
        raise

    output = ssm.get_command_invocation(
        CommandId=command_id,
        InstanceId=instance_id
    )
    
    print(f"Command output:\n{output.get('StandardOutputContent', '')}")

    if output['Status'] == 'Success':
        print(f"Command status: Success")
    
    if output['Status'] != 'Success':
        print(f"Command failed: {script_content}")
        print(f"Error: {output['StandardErrorContent']}")
        raise Exception(f"Command execution failed: {script_content}")

def get_instance_id_by_name(region, instance_name):

    ec2 = boto3.client('ec2', region_name=region)
    response = ec2.describe_instances(
        Filters=[
            {
                'Name': 'tag:Name',
                'Values': [instance_name]
            },
            {
                'Name': 'instance-state-name',
                'Values': ['running']
            }
        ]
    )

    for reservation in response['Reservations']:
        for instance in reservation['Instances']:
            return instance['InstanceId']
    
    return None

region = get_value("AWS_REGION")
instance_name = data["windows_instance_tag"]

instance_id = get_instance_id_by_name(region, instance_name)
script_path = os.path.join(os.path.dirname(__file__), 'gmsa.ps1')

run_powershell_script(instance_id, script_path)

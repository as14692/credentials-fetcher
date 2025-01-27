from aws_cdk import (
    # Duration,
    Stack,
    # aws_sqs as sqs,
    aws_rds as rds
)
from constructs import Construct
import aws_cdk.aws_directoryservice as directoryservice
import aws_cdk.aws_ec2 as ec2
import aws_cdk.aws_secretsmanager as secretsmanager
import aws_cdk.aws_autoscaling as autoscaling
import aws_cdk as cdk
import json
import base64
import aws_cdk.aws_route53 as route53
import aws_cdk.aws_ecs as ecs
import aws_cdk.aws_iam as iam
import aws_cdk.aws_ssm as ssm
from aws_cdk import aws_route53resolver as route53resolver
from aws_cdk import Duration as duration
import uuid
import boto3
import docker
import os

class CdkStack(Stack):

    vpc = None
    security_group = None
    subnets = None
    cfn_microsoft_AD = None
    prefix_list = None
    password = None
    key_pair = None
    subnet_1 = None
    subnet_2 = None

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs, description="Test Credentials-fetcher in domainless mode")

    def init_vpc(self, prefix_list: str, key_pair_name: str, stack_name: str):
        vpc_name = stack_name + "-vpc"
        # Define the VPC
        self.vpc = ec2.Vpc(
                    self,
                    id=vpc_name,
                    ip_addresses=ec2.IpAddresses.cidr("10.0.0.0/16"),
                    vpc_name=vpc_name,  # Move vpc_name before cidr
                    max_azs=2,  # Number of Availability Zones to use
                    subnet_configuration=[
                        ec2.SubnetConfiguration(
                            cidr_mask=24,  # Subnet mask for public subnets
                            name="SubnetConfig1",
                            subnet_type=ec2.SubnetType.PUBLIC,
                        ),
                        ec2.SubnetConfiguration(
                            cidr_mask=24,  # Subnet mask for public subnets
                            name="SubnetConfig2",
                            subnet_type=ec2.SubnetType.PUBLIC,
                        )
                    ],
                )

        # get AWS availability zones in the region
        availability_zones = self.availability_zones
        # get first availability zone
        first_availability_zone = availability_zones[0]
        # get second availability zone
        second_availability_zone = availability_zones[1]
        # Create two subnets in the VPC
        self.subnet_1 = self.vpc.public_subnets[0]

        self.subnet_2 = self.vpc.public_subnets[1]

        self.subnets = [self.subnet_1, self.subnet_2]
        self.security_group = ec2.SecurityGroup(self,
                                 vpc=self.vpc,
                                 allow_all_outbound=True,
                                 description=stack_name + "-Security Group",
                                 id=stack_name + "-SecurityGroup"
                                 )
        self.prefix_list =  ec2.Peer.prefix_list(prefix_list)

        self.security_group.add_ingress_rule (self.prefix_list,
                                                 ec2.Port.all_traffic())
        
        self.security_group.add_ingress_rule(
            peer=self.security_group,
            connection=ec2.Port.all_traffic(),
            description="Allow all traffic from self"
        )


        # Import existing keypair using keyname
        self.key_pair = ec2.KeyPair.from_key_pair_name(self, "KeyPair", key_pair_name)
        return

    def init_route53_endpoint(self, domain_name, vpc):

        # create route53 endpoint
        endpoint = route53resolver.CfnResolverEndpoint(self, "CredentialsFetcherResolverEndpoint",
                                            direction="OUTBOUND",
                                            name="resolver",
                                            ip_addresses=[
                                                route53resolver.CfnResolverEndpoint.IpAddressRequestProperty(
                                                subnet_id=self.subnet_1.subnet_id),
                                                route53resolver.CfnResolverEndpoint.IpAddressRequestProperty(
                                                subnet_id=self.subnet_2.subnet_id)
                                            ],
                                            security_group_ids = [self.security_group.security_group_id]
                                        )

        # Create resolver forwarding rule
        resolver_rule = route53resolver.CfnResolverRule(self, "CredentialsFetcherResolverRule",
                                                        domain_name=domain_name,
                                                        rule_type="FORWARD",
                                                        resolver_endpoint_id=endpoint.attr_resolver_endpoint_id,
                                                        target_ips=[route53resolver.CfnResolverRule.TargetAddressProperty(
                                                            # First dc_ip_address
                                                            ip = cdk.Fn.select(0, self.cfn_microsoft_AD.attr_dns_ip_addresses)
                                                        ),
                                                        route53resolver.CfnResolverRule.TargetAddressProperty(
                                                            ip = cdk.Fn.select(1, self.cfn_microsoft_AD.attr_dns_ip_addresses)
                                                        )],
                                                        )

         # Associate the Resolver Rule with the VPC
        route53resolver.CfnResolverRuleAssociation(
            self,
            "CredentialsFetcherResolverRuleAssociation",
            resolver_rule_id=resolver_rule.ref,
            vpc_id=vpc.vpc_id,
        )

        resolver_rule.node.add_dependency(vpc)
        resolver_rule.node.add_dependency(self.cfn_microsoft_AD)

    def init_DirectoryService(self, directory_name:str, domain_admin_password: str):
        self.password = domain_admin_password

        # Get subnet_ids from vpc.public_subnets
        subnet_ids = [self.subnet_1.subnet_id, self.subnet_2.subnet_id]
        self.cfn_microsoft_AD = directoryservice.CfnMicrosoftAD(
                                    self,
                                    directory_name,
                                    name=directory_name,
                                    password=domain_admin_password,
                                    vpc_settings=directoryservice.CfnMicrosoftAD.VpcSettingsProperty(
                                        subnet_ids=subnet_ids,
                                        vpc_id=self.vpc.vpc_id
                                    ),
                                    # the properties below are optional
                                    create_alias=False,
                                    edition="Standard",
                                    enable_sso=False
                                )


        self.cfn_microsoft_AD.node.add_dependency(self.vpc)

        return self.cfn_microsoft_AD

    def launch_windows_instance(self, instance_tag: str, password: str,
                                domain_name: str,
                                key_name: str,
                                number_of_gmsa_accounts: int,
                                s3_bucket_name: str):

        # Add user_data_script to user_data
        user_data = ec2.UserData.for_windows(persist=True)
        user_data = cdk.Fn.base64(user_data.render())

        # Create an instance role
        role = iam.Role(
            self, "InstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonS3FullAccess"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMDirectoryServiceAccess"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AWSDirectoryServiceFullAccess"),
                iam.ManagedPolicy.from_aws_managed_policy_name("SecretsManagerReadWrite"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
            ],
        )

        # https://github.com/aws/aws-cdk/issues/4057
        # Domain-join works if SSM agent is reachable and SSM association can pick it up
        association = cdk.CfnResource(
                        self, "Association",
                        type="AWS::SSM::Association",
                        properties={
                        "Targets": [
                            {
                                "Key": "tag:Name",
                                "Values": [instance_tag]
                            }
                        ],
                        "Parameters": {
                            "directoryName": [domain_name],
                            "directoryId": [self.cfn_microsoft_AD.ref]
                        },
                        "Name": "AWS-JoinDirectoryServiceDomain"
                        }
                    )
        association.node.add_dependency(self.cfn_microsoft_AD)

        iam_instance_profile = iam.CfnInstanceProfile(self, "InstanceProfile", roles=[role.role_name])
        instance = ec2.CfnInstance(
                    self,
                    "MyCfnInstance",
                    instance_type=ec2.InstanceType.of(ec2.InstanceClass.BURSTABLE3, ec2.InstanceSize.XLARGE).to_string(),
                    image_id=ec2.WindowsImage(version=ec2.WindowsVersion.WINDOWS_SERVER_2022_ENGLISH_FULL_SQL_2022_ENTERPRISE).get_image(self).image_id,
                    security_group_ids=[self.security_group.security_group_id],
                    subnet_id=self.subnet_1.subnet_id,
                    tags=[cdk.CfnTag(key="Name", value=instance_tag)],
                    key_name = key_name,
                    iam_instance_profile = iam_instance_profile.ref
                    )
        instance_id = instance.ref

        instance.node.add_dependency(self.cfn_microsoft_AD)
        instance.node.add_dependency(self.vpc)
        return instance

    def setup_windows_userdata(self, password:str, domain_name:str, number_of_gmsa_accounts:int, s3_bucket_name: str):

        userdata_script = ""
        # Read contents of the file gmsa.ps1
        with open("gmsa.ps1", "r") as f:
            userdata_script = f.read()
# Get-ADServiceAccount -Credential $credential -Server ActiveDirectory1.com -Identity "WebApp1"
# SSM agent log in windows is at C:\ProgramData\Amazon\EC2Launch\log\agent.log in Windows-2022

        userdata_script = userdata_script.replace("INPUTPASSWORD", password)
        userdata_script = userdata_script.replace("DOMAINNAME", domain_name)
        netbios_domain_name = domain_name.split(".")[0]
        userdata_script = userdata_script.replace("NETBIOS_NAME", netbios_domain_name)
        userdata_script = userdata_script.replace("NUMBER_OF_GMSA_ACCOUNTS", str(number_of_gmsa_accounts))
        userdata_script = userdata_script.replace("BUCKET_NAME", s3_bucket_name)

        return userdata_script


    def create_ecs_cluster(self, cluster_name: str,
                                instance_tag: str, password: str,
                                domain_name: str,
                                key_pair: ec2.KeyPair,
                                number_of_gmsa_accounts: int,
                                vpc : str,
                                security_group : str, rpm_file:str, s3_bucket:str):

        machine_image = ecs.EcsOptimizedImage.amazon_linux2023(hardware_type=ecs.AmiHardwareType.STANDARD)
        instance_type=ec2.InstanceType.of(ec2.InstanceClass.BURSTABLE3, ec2.InstanceSize.XLARGE)
        role = iam.Role(self, "Role", role_name="ecs-instance-role", assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"))

        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonEC2ContainerServiceforEC2Role"))
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AmazonECS_FullAccess"))
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("SecretsManagerReadWrite"))
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMFullAccess"))
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AmazonS3FullAccess"))
        # add role for Directory Service
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AWSDirectoryServiceFullAccess"))

        user_data_script = self.setup_linux_userdata(instance_tag, password, domain_name, key_pair.key_pair_name, number_of_gmsa_accounts, rpm_file, s3_bucket)
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(user_data_script)
        #user_data = cdk.Fn.base64(user_data.render())

        subnet_selection = ec2.SubnetSelection(
                                subnet_type=ec2.SubnetType.PUBLIC
                            )
        auto_scaling_group = autoscaling.AutoScalingGroup(self, "MyAutoScalingGroup",
                                                           vpc=vpc, require_imdsv2 = True,
                                                           min_capacity=1,
                                                           max_capacity=1,
                                                           desired_capacity=1,
                                                           security_group = security_group,
                                                           machine_image = machine_image,
                                                           instance_type = instance_type,
                                                           key_pair = key_pair,
                                                           role = role,
                                                           associate_public_ip_address = True,
                                                           vpc_subnets = subnet_selection,
                                                           user_data = user_data
                                                           )
        capacity_provider = ecs.AsgCapacityProvider(self, "MyCapacityProvider",
                                                    auto_scaling_group=auto_scaling_group,
                                                    capacity_provider_name="MyCapacityProvider",
                                                    target_capacity_percent=100)

        cluster = ecs.Cluster(self, "MyCluster",
                              cluster_name=cluster_name,
                              container_insights=True,
                              vpc=self.vpc
                              )
        cluster.add_asg_capacity_provider(capacity_provider)

        return cluster

    def setup_linux_userdata (self, instance_tag: str, password: str,
                                domain_name: str,
                                key_name: str,
                                number_of_gmsa_accounts: int, rpm_file: str, s3_bucket: str):
        #In instance, 'cat /var/lib/cloud/instance/user-data.txt'
        # get random uuid string
        random_uuid_str =  str(uuid.uuid4())
        ecs_cluster_name="ecs-load-test-" + random_uuid_str
        user_data_script = '''
            echo "ECS_GMSA_SUPPORTED=true" >> /etc/ecs/ecs.config
            dnf install -y dotnet
            dnf install -y realmd
            dnf install -y oddjob
            dnf install -y oddjob-mkhomedir
            dnf install -y sssd
            dnf install -y adcli
            dnf install -y krb5-workstation
            dnf install -y samba-common-tools
            if aws s3 ls "s3://BUCKET_NAME/RPM_FILE" &> /dev/null; then
                echo "RPM file found in S3 bucket. Transferring to EC2 instance..." >> /tmp/userdata.log
                aws s3 cp s3://BUCKET_NAME/RPM_FILE .
                dnf install -y ./RPM_FILE
                if [ $? -ne 0 ]; then
                    echo "RPM file installation failed. Installing credentials-fetcher..." >> /tmp/userdata.log
                    dnf install -y credentials-fetcher
                else
                    echo "RPM file installation successful." >> /tmp/userdata.log
                fi
            else
                echo "RPM file not found in S3 bucket. Installing credentials-fetcher..." >> /tmp/userdata.log
                dnf install -y credentials-fetcher
            fi
            systemctl enable credentials-fetcher
            systemctl start credentials-fetcher
            systemctl enable --now --no-block ecs.service
        user_data_script += "echo ECS_CLUSTER=" + ecs_cluster_name + " >> /etc/ecs/ecs.config"
        '''
        user_data_script = user_data_script.replace('BUCKET_NAME', s3_bucket)
        user_data_script = user_data_script.replace('RPM_FILE', rpm_file)
        return user_data_script

    # Save json values in secrets manager
    def save_json_values_in_secrets_manager(self, secret_name: str, data: str):
        secretsmanager = boto3.client('secretsmanager')
        response = secretsmanager.create_secret(
            Name=secret_name,
            SecretString=json.dumps(data)
        )

    def create_task_definition(self, task_definition_template_name):
        role = iam.Role(self, "CredentialsFetcher-ECSTaskExecutionRolegMSA", role_name="CredentialsFetcher-ECSTaskExecutionRolegMSA",
                            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("SecretsManagerReadWrite"))
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AmazonS3FullAccess"))
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AWSDirectoryServiceFullAccess"))
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMFullAccess"))
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AmazonECS_FullAccess"))
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEC2ContainerRegistryFullAccess"))
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AmazonElasticContainerRegistryPublicFullAccess"))

         # Create the policy statement
        ssm_messages_policy_statement = iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["ssmmessages:CreateControlChannel"],
            resources=["*"]
        )

        # Create the policy statement
        ssm_messages_policy_statement = iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["ssmmessages:CreateDataChannel"],
            resources=["*"]
        )
         # Create the policy statement
        ssm_messages_policy_statement = iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["ssmmessages:OpenControlChannel"],
            resources=["*"]
        )

        # Create the policy statement
        ssm_messages_policy_statement = iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["ssmmessages:OpenDataChannel"],
            resources=["*"]
        )
        role.add_to_principal_policy(ssm_messages_policy_statement)

        # Create task definition
        task_definition = ecs.TaskDefinition(self, task_definition_template_name,
                                            compatibility=ecs.Compatibility.EC2_AND_FARGATE,
                                            cpu="1024",
                                            memory_mib="2048",
                                            task_role=role,
                                            execution_role=role
                                            )

        container_definition = task_definition.add_container(
            "MyContainer",
            image=ecs.ContainerImage.from_registry("nginx:latest"),
            memory_reservation_mib=128,
            start_timeout=duration.seconds(120),
            stop_timeout=duration.seconds(60)
        )
        # Add credspecs using boto

        task_definition.node.add_dependency(role)
        task_definition.node.add_dependency(self.vpc)
        task_definition.node.add_dependency(self.security_group)
        task_definition.node.add_dependency(self.cfn_microsoft_AD)

        return task_definition

    def build_push_dockerfile_to_ecr(self, dockerfile_path, repository_name, region, tag='latest'):
        ecr_client = boto3.client('ecr', region_name=region)
        
        # Create ECR repository if it doesn't exist
        try:
            ecr_client.create_repository(repositoryName=repository_name)
        except ecr_client.exceptions.RepositoryAlreadyExistsException:
            pass

        # Get the repository URI
        response = ecr_client.describe_repositories(repositoryNames=[repository_name])
        repository_uri = response['repositories'][0]['repositoryUri']

        # Get ECR login token
        token = ecr_client.get_authorization_token()
        username, password = base64.b64decode(token['authorizationData'][0]['authorizationToken']).decode().split(':')
        registry = token['authorizationData'][0]['proxyEndpoint']

        # Build Docker image
        docker_client = docker.from_env()
        image, build_logs = docker_client.images.build(
            path=os.path.dirname(dockerfile_path),
            dockerfile=os.path.basename(dockerfile_path),
            tag=f"{repository_uri}:{tag}"
        )

        # Login to ECR
        docker_client.login(username=username, password=password, registry=registry)

        # Push image to ECR
        push_logs = docker_client.images.push(repository_uri, tag=tag)

        # Construct the full image URI
        image_uri = f"{repository_uri}:{tag}"

        return image_uri
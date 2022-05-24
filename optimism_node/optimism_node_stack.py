from aws_cdk import Stack
from aws_cdk import aws_autoscaling as autoscaling
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct


class OptimismNodeStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.IVpc,
        basic_auth_username: str,
        basic_auth_hashed_password: str,
        l1_node_url: str,
        l2_node_url: str,
        **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        cluster = ecs.Cluster(self, "OptimismNodeCluster", vpc=vpc)

        asg_provider = ecs.AsgCapacityProvider(
            self,
            "OptimismNodeAsgCapacityProvider",
            auto_scaling_group=autoscaling.AutoScalingGroup(
                self,
                "OptimismNodeAsg",
                instance_type=ec2.InstanceType("i3en.large"),
                vpc=vpc,
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
                machine_image=ecs.EcsOptimizedImage.amazon_linux2(),
            ),
        )

        cluster.add_asg_capacity_provider(asg_provider)

        asg_provider.auto_scaling_group.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "AmazonSSMManagedInstanceCore"
            )
        )

        asg_provider.auto_scaling_group.connections.allow_from_any_ipv4(
            ec2.Port.tcp(80)
        )
        asg_provider.auto_scaling_group.connections.allow_from_any_ipv4(
            ec2.Port.tcp(443)
        )

        cluster.add_asg_capacity_provider(asg_provider)

        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            *[
                "sudo mkdir -p /mnt/nvm/",
                "sudo mkfs -t ext4 /dev/nvme1n1",
                "sudo mount -t ext4 /dev/nvme1n1 /mnt/nvm",
                "sudo mkdir -p /mnt/nvm/nodedata",
                "sudo chown ec2-user:ec2-user /mnt/nvm/nodedata",
            ]
        )
        asg_provider.auto_scaling_group.add_user_data(user_data.render())

        log_group = logs.LogGroup(
            self,
            "LogGroup",
            retention=logs.RetentionDays.ONE_WEEK,
        )

        # permit EC2 task to read envfiles from S3
        s3_policy_statement = iam.PolicyStatement(
            actions=["s3:GetBucketLocation", "s3:GetObject"]
        )

        s3_policy_statement.add_all_resources()

        execution_role = iam.Role(
            self,
            "ExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            inline_policies={
                "s3Policy": iam.PolicyDocument(
                    statements=[s3_policy_statement],
                ),
            },
        )

        task_definition = ecs.Ec2TaskDefinition(
            self,
            "OptimismNodeTaskDefinition",
            network_mode=ecs.NetworkMode.BRIDGE,
            execution_role=execution_role,
            volumes=[
                ecs.Volume(
                    name="optimism-dtl",
                    host=ecs.Host(source_path="/mnt/nvm/nodedata/optimism/mainnet/dtl"),
                ),
                ecs.Volume(
                    name="optimism-geth",
                    host=ecs.Host(
                        source_path="/mnt/nvm/nodedata/optimism/mainnet/geth"
                    ),
                ),
            ],
        )

        default_env_vars = {
            "ETH_NETWORK": "mainnet",
            "DATA_TRANSPORT_LAYER__L2_RPC_ENDPOINT": "https://mainnet.optimism.io",
            "REPLICA_HEALTHCHECK__ETH_NETWORK_RPC_PROVIDER": "https://mainnet.optimism.io",
            "SEQUENCER_CLIENT_HTTP": "https://mainnet.optimism.io",
            "GCMODE": "archive",
            "L2GETH_HTTP_PORT": "9991",
            "L2GETH_WS_PORT": "9992",
            "DTL_PORT": "7878",
            "DATA_TRANSPORT_LAYER__L1_RPC_ENDPOINT": l1_node_url,
            "GETH_INIT_SCRIPT": "check-for-chaindata-berlin.sh",
            "ETH_NETWORK": "mainnet",
        }

        dtl = task_definition.add_container(
            "OptimismDataTransportLayerContainer",
            container_name="optimism-dtl",
            image=ecs.ContainerImage.from_registry(
                "ethereumoptimism/data-transport-layer:0.5.20"
            ),
            memory_reservation_mib=1024,
            logging=ecs.AwsLogDriver(
                log_group=log_group,
                stream_prefix="optimism-dtl",
                mode=ecs.AwsLogDriverMode.NON_BLOCKING,
            ),
            environment=default_env_vars,
            environment_files=[
                ecs.EnvironmentFile.from_asset("./envs/data-transport-layer.env")
            ],
            port_mappings=[
                ecs.PortMapping(container_port=7878, host_port=7878),
            ],
        )

        dtl.add_mount_points(
            ecs.MountPoint(
                source_volume="optimism-dtl",
                container_path="/db",
                read_only=False,
            )
        )

        l2geth_replica = task_definition.add_container(
            "OptimismGethReplicaContainer",
            container_name="optimism-geth",
            image=ecs.ContainerImage.from_asset("docker/l2geth"),
            memory_reservation_mib=1024,
            logging=ecs.AwsLogDriver(
                log_group=log_group,
                stream_prefix="optimism-l2geth-replica",
                mode=ecs.AwsLogDriverMode.NON_BLOCKING,
            ),
            entry_point=[
                "/bin/sh",
                "-c",
                "/scripts/$GETH_INIT_SCRIPT && /scripts/l2geth-replica-start.sh --config /scripts/l2geth.toml",
            ],
            environment=default_env_vars,
            environment_files=[
                ecs.EnvironmentFile.from_asset("./envs/l2geth-replica.env")
            ],
            port_mappings=[
                ecs.PortMapping(container_port=9991, host_port=8545),
                ecs.PortMapping(container_port=9992, host_port=8546),
            ],
        )

        l2geth_replica.add_link(dtl, "data-transport-layer")

        l2geth_replica.add_mount_points(
            ecs.MountPoint(
                source_volume="optimism-geth",
                container_path="/geth",
                read_only=False,
            )
        )

        caddy_container = task_definition.add_container(
            "CaddyContainer",
            container_name="caddy",
            image=ecs.ContainerImage.from_asset("docker/caddy"),
            memory_reservation_mib=1024,
            logging=ecs.AwsLogDriver(
                log_group=log_group,
                stream_prefix="caddy",
                mode=ecs.AwsLogDriverMode.NON_BLOCKING,
            ),
            port_mappings=[
                ecs.PortMapping(container_port=80, host_port=80),
                ecs.PortMapping(container_port=443, host_port=443),
            ],
            environment={
                "BASICAUTH_USERNAME": basic_auth_username,
                "BASICAUTH_HASHED_PASSWORD": basic_auth_hashed_password,
            },
        )

        caddy_container.add_link(l2geth_replica, "optimism")

        service = ecs.Ec2Service(
            self,
            "OptimismNodeService",
            cluster=cluster,
            task_definition=task_definition,
        )

        service.task_definition.task_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=[
                    "route53:ListResourceRecordSets",
                    "route53:GetChange",
                    "route53:ChangeResourceRecordSets",
                ],
                resources=[
                    "arn:aws:route53:::hostedzone/*",
                    "arn:aws:route53:::change/*",
                ],
            )
        )

        service.task_definition.task_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["route53:ListHostedZonesByName", "route53:ListHostedZones"],
                resources=["*"],
            )
        )

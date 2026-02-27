"""CDK Stack for Fault Localization infrastructure."""

from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecs_patterns as ecs_patterns,
    aws_opensearchservice as opensearch,
    aws_s3 as s3,
    aws_iam as iam,
    aws_logs as logs,
)
from constructs import Construct


class FaultLocalizationStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # VPC
        vpc = ec2.Vpc(
            self, "FaultLocVpc",
            max_azs=2,
            nat_gateways=1
        )

        # S3 Bucket for codebase storage
        codebase_bucket = s3.Bucket(
            self, "CodebaseBucket",
            bucket_name=f"fault-loc-codebase-{self.account}",
            removal_policy=RemovalPolicy.RETAIN,
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED
        )

        # OpenSearch Domain
        opensearch_domain = opensearch.Domain(
            self, "FaultLocSearch",
            version=opensearch.EngineVersion.OPENSEARCH_2_11,
            vpc=vpc,
            vpc_subnets=[ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)],
            capacity=opensearch.CapacityConfig(
                data_node_instance_type="r6g.large.search",
                data_nodes=2
            ),
            ebs=opensearch.EbsOptions(
                volume_size=100,
                volume_type=ec2.EbsDeviceVolumeType.GP3
            ),
            zone_awareness=opensearch.ZoneAwarenessConfig(
                availability_zone_count=2
            ),
            removal_policy=RemovalPolicy.RETAIN,
            encryption_at_rest=opensearch.EncryptionAtRestOptions(enabled=True),
            node_to_node_encryption=True,
            enforce_https=True
        )

        # ECS Cluster
        cluster = ecs.Cluster(
            self, "FaultLocCluster",
            vpc=vpc,
            container_insights=True
        )

        # Task Role with permissions
        task_role = iam.Role(
            self, "TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com")
        )

        # S3 read access
        codebase_bucket.grant_read(task_role)

        # Bedrock access
        task_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=["*"]
        ))

        # OpenSearch access
        task_role.add_to_policy(iam.PolicyStatement(
            actions=["es:ESHttp*"],
            resources=[f"{opensearch_domain.domain_arn}/*"]
        ))

        # Fargate Service with ALB
        fargate_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self, "FaultLocService",
            cluster=cluster,
            cpu=1024,
            memory_limit_mib=4096,
            desired_count=2,
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ecs.ContainerImage.from_asset("../../"),
                container_port=8080,
                task_role=task_role,
                environment={
                    "OPENSEARCH_HOST": opensearch_domain.domain_endpoint,
                    "OPENSEARCH_PORT": "443",
                    "USE_LLM": "true",
                    "AWS_REGION": self.region,
                    "CODEBASE_BUCKET": codebase_bucket.bucket_name
                },
                log_driver=ecs.LogDrivers.aws_logs(
                    stream_prefix="fault-loc",
                    log_retention=logs.RetentionDays.ONE_WEEK
                )
            ),
            public_load_balancer=True
        )

        # Health check
        fargate_service.target_group.configure_health_check(
            path="/health",
            healthy_http_codes="200"
        )

        # Auto-scaling
        scaling = fargate_service.service.auto_scale_task_count(
            min_capacity=1,
            max_capacity=10
        )
        scaling.scale_on_cpu_utilization(
            "CpuScaling",
            target_utilization_percent=70
        )

        # Allow ECS to access OpenSearch
        opensearch_domain.connections.allow_from(
            fargate_service.service,
            ec2.Port.tcp(443)
        )

        # Outputs
        CfnOutput(self, "ApiUrl", value=fargate_service.load_balancer.load_balancer_dns_name)
        CfnOutput(self, "OpenSearchEndpoint", value=opensearch_domain.domain_endpoint)
        CfnOutput(self, "CodebaseBucketName", value=codebase_bucket.bucket_name)

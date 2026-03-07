"""CDK Stack for Fault Localization infrastructure (prototype).

Uses self-hosted Elasticsearch on Fargate instead of managed OpenSearch
to avoid subscription requirements during prototyping.
"""

from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecs_patterns as ecs_patterns,
    aws_ecr as ecr,
    aws_s3 as s3,
    aws_iam as iam,
    aws_logs as logs,
    aws_servicediscovery as sd,
)
from constructs import Construct


class FaultLocalizationStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # VPC (minimal for prototype)
        vpc = ec2.Vpc(
            self, "FaultLocVpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
            ]
        )

        # S3 Bucket for codebase storage
        codebase_bucket = s3.Bucket(
            self, "CodebaseBucket",
            bucket_name=f"fault-loc-codebase-{self.account}",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
        )

        # ECS Cluster with Cloud Map namespace for service discovery
        cluster = ecs.Cluster(
            self, "FaultLocCluster",
            vpc=vpc,
            default_cloud_map_namespace=ecs.CloudMapNamespaceOptions(
                name="faultloc.local",
                type=sd.NamespaceType.DNS_PRIVATE,
            ),
        )

        # --- Elasticsearch Fargate Service (replaces managed OpenSearch) ---
        es_task_def = ecs.FargateTaskDefinition(
            self, "EsTaskDef",
            cpu=512,
            memory_limit_mib=1024,
        )

        es_container = es_task_def.add_container(
            "elasticsearch",
            image=ecs.ContainerImage.from_registry("opensearchproject/opensearch:2.11.0"),
            environment={
                "discovery.type": "single-node",
                "DISABLE_SECURITY_PLUGIN": "true",
                "OPENSEARCH_JAVA_OPTS": "-Xms384m -Xmx384m",
            },
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="elasticsearch",
                log_retention=logs.RetentionDays.ONE_WEEK,
            ),
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL", "curl -f http://localhost:9200/_cluster/health || exit 1"],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(10),
                retries=5,
                start_period=Duration.seconds(90),
            ),
        )
        es_container.add_port_mappings(ecs.PortMapping(container_port=9200))

        es_sg = ec2.SecurityGroup(self, "EsSg", vpc=vpc, description="Elasticsearch SG")
        es_sg.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(9200))

        es_service = ecs.FargateService(
            self, "EsService",
            cluster=cluster,
            task_definition=es_task_def,
            desired_count=1,
            assign_public_ip=True,
            security_groups=[es_sg],
            cloud_map_options=ecs.CloudMapOptions(
                name="elasticsearch",
                dns_record_type=sd.DnsRecordType.A,
                dns_ttl=Duration.seconds(30),
            ),
        )

        # --- Task Role for the app ---
        task_role = iam.Role(
            self, "TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        codebase_bucket.grant_read(task_role)
        task_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=["*"],
        ))

        # ECR Repository for app image
        ecr_repo = ecr.Repository(
            self, "FaultLocRepo",
            repository_name="fault-localization",
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
        )

        # --- App Fargate Service with ALB ---
        fargate_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self, "FaultLocService",
            cluster=cluster,
            cpu=1024,
            memory_limit_mib=6144,
            desired_count=1,
            assign_public_ip=True,
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ecs.ContainerImage.from_ecr_repository(ecr_repo, tag="latest"),
                container_port=8080,
                task_role=task_role,
                environment={
                    "OPENSEARCH_HOST": "elasticsearch.faultloc.local",
                    "OPENSEARCH_PORT": "9200",
                    "USE_LLM": "true",
                    "AWS_REGION": self.region,
                    "CODEBASE_BUCKET": codebase_bucket.bucket_name,
                },
                log_driver=ecs.LogDrivers.aws_logs(
                    stream_prefix="fault-loc",
                    log_retention=logs.RetentionDays.ONE_WEEK,
                ),
            ),
            public_load_balancer=True,
        )

        fargate_service.target_group.configure_health_check(
            path="/health",
            healthy_http_codes="200",
            interval=Duration.seconds(60),
            timeout=Duration.seconds(10),
            healthy_threshold_count=2,
            unhealthy_threshold_count=5,
        )

        scaling = fargate_service.service.auto_scale_task_count(min_capacity=1, max_capacity=2)
        scaling.scale_on_cpu_utilization("CpuScaling", target_utilization_percent=80)

        # Outputs
        CfnOutput(self, "ApiUrl", value=fargate_service.load_balancer.load_balancer_dns_name)
        CfnOutput(self, "ElasticsearchEndpoint", value="elasticsearch.faultloc.local:9200")
        CfnOutput(self, "CodebaseBucketName", value=codebase_bucket.bucket_name)
        CfnOutput(self, "EcrRepoUri", value=ecr_repo.repository_uri)

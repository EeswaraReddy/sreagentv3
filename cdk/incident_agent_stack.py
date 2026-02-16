"""CDK Stack for Incident Agent Infrastructure."""
from aws_cdk import (
    Stack,
    Duration,
    aws_lambda as lambda_,
    aws_events as events,
    aws_events_targets as targets,
    aws_s3 as s3,
    aws_iam as iam,
    aws_secretsmanager as secretsmanager,
    aws_logs as logs,
    RemovalPolicy,
)
from constructs import Construct


class IncidentAgentStack(Stack):
    """CDK Stack for deploying the SRE Incident Agent system."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # =================================================================
        # S3 Bucket for RCA Storage
        # =================================================================
        rca_bucket = s3.Bucket(
            self,
            "RCABucket",
            bucket_name=f"incident-rca-{self.account}-{self.region}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="archive-old-rcas",
                    enabled=True,
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER,
                            transition_after=Duration.days(90)
                        )
                    ]
                )
            ]
        )

        # =================================================================
        # Secrets Manager for ServiceNow Credentials
        # =================================================================
        servicenow_secret = secretsmanager.Secret(
            self,
            "ServiceNowSecret",
            secret_name="servicenow/credentials",
            description="ServiceNow API credentials",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"servicenow_instance":"your-instance.service-now.com","servicenow_username":"service-account"}',
                generate_string_key="servicenow_password"
            )
        )

        # =================================================================
        # IAM Role for Lambda Functions
        # =================================================================
        lambda_role = iam.Role(
            self,
            "IncidentAgentLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ]
        )

        # Grant permissions
        rca_bucket.grant_read_write(lambda_role)
        servicenow_secret.grant_read(lambda_role)

        # Bedrock permissions
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream"
                ],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/*"
                ]
            )
        )

        # Lambda invoke permissions (for poller to invoke orchestrator)
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[f"arn:aws:lambda:{self.region}:{self.account}:function:incident-*"]
            )
        )

        # CloudWatch Logs permissions
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:GetLogEvents",
                    "logs:FilterLogEvents"
                ],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/*/mwaa/*",
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws-glue/*"
                ]
            )
        )

        # Glue permissions (for retry actions)
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "glue:StartJobRun",
                    "glue:GetJobRun",
                    "glue:GetJob"
                ],
                resources=[f"arn:aws:glue:{self.region}:{self.account}:job/*"]
            )
        )

        # =================================================================
        # Lambda Layer for Dependencies
        # =================================================================
        dependencies_layer = lambda_.LayerVersion(
            self,
            "DependenciesLayer",
            code=lambda_.Code.from_asset("../lambda_layer"),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_11],
            description="Strands, Boto3, and other dependencies"
        )

        # =================================================================
        # Lambda Function: Orchestrator
        # =================================================================
        orchestrator_lambda = lambda_.Function(
            self,
            "OrchestratorFunction",
            function_name="incident-orchestrator",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../lambdas/orchestrator"),
            role=lambda_role,
            timeout=Duration.minutes(15),
            memory_size=1024,
            environment={
                "RCA_BUCKET": rca_bucket.bucket_name,
                "MCP_GATEWAY_ENDPOINT": "",  # TODO: Add MCP Gateway endpoint
                "MODEL_ID": "us.anthropic.claude-sonnet-4-20250514-v1:0",
                "LOG_LEVEL": "INFO"
            },
            layers=[dependencies_layer],
            log_retention=logs.RetentionDays.ONE_MONTH
        )

        # =================================================================
        # Lambda Function: Poller
        # =================================================================
        poller_lambda = lambda_.Function(
            self,
            "PollerFunction",
            function_name="incident-poller",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../lambdas/poller"),
            role=lambda_role,
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "ASSIGNMENT_GROUP": "Data Lake Platform Team",
                "ORCHESTRATOR_LAMBDA": orchestrator_lambda.function_name,
                "POLL_LIMIT": "10",
                "MINUTES_BACK": "10",
                "LOG_LEVEL": "INFO"
            },
            layers=[dependencies_layer],
            log_retention=logs.RetentionDays.ONE_MONTH
        )

        # =================================================================
        # EventBridge Rule for Polling
        # =================================================================
        polling_rule = events.Rule(
            self,
            "PollingRule",
            rule_name="incident-poller-schedule",
            description="Trigger incident poller every 5 minutes",
            schedule=events.Schedule.rate(Duration.minutes(5)),
            enabled=True
        )

        polling_rule.add_target(
            targets.LambdaFunction(poller_lambda)
        )

        # =================================================================
        # CloudWatch Alarms (for monitoring)
        # =================================================================
        # TODO: Add CloudWatch alarms for:
        # - Lambda errors
        # - Lambda duration
        # - Incident processing failures

        # =================================================================
        # Outputs
        # =================================================================
        from aws_cdk import CfnOutput

        CfnOutput(
            self,
            "RCABucketName",
            value=rca_bucket.bucket_name,
            description="S3 bucket for RCA storage"
        )

        CfnOutput(
            self,
            "OrchestratorFunctionName",
            value=orchestrator_lambda.function_name,
            description="Orchestrator Lambda function name"
        )

        CfnOutput(
            self,
            "PollerFunctionName",
            value=poller_lambda.function_name,
            description="Poller Lambda function name"
        )

        CfnOutput(
            self,
            "SecretARN",
            value=servicenow_secret.secret_arn,
            description="ServiceNow credentials secret ARN"
        )

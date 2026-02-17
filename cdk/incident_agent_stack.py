"""CDK Stack for Incident Agent Infrastructure.

Deploys:
  - 13 tool Lambda functions (behind AgentCore Gateway)
  - Orchestrator Lambda (Strands Agent)
  - Poller Lambda (ServiceNow polling)
  - S3 bucket for RCA storage
  - EventBridge rule for periodic polling
  - IAM roles with least-privilege permissions

Architecture:
  EventBridge -> Poller -> Orchestrator -> MCPClient -> AgentCore Gateway (IAM) -> Tool Lambdas
"""
from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
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
        # IAM Role for Tool Lambda Functions
        # =================================================================
        tool_lambda_role = iam.Role(
            self,
            "ToolLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ]
        )

        # EMR permissions (get_emr_logs, retry_emr)
        tool_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "elasticmapreduce:DescribeCluster",
                    "elasticmapreduce:DescribeStep",
                    "elasticmapreduce:ListSteps",
                    "elasticmapreduce:AddJobFlowSteps",
                ],
                resources=[f"arn:aws:elasticmapreduce:{self.region}:{self.account}:cluster/*"]
            )
        )

        # Glue permissions (get_glue_logs, retry_glue_job)
        tool_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "glue:GetJob",
                    "glue:GetJobRun",
                    "glue:GetJobRuns",
                    "glue:StartJobRun",
                ],
                resources=[f"arn:aws:glue:{self.region}:{self.account}:job/*"]
            )
        )

        # Athena permissions (get_athena_query, retry_athena_query)
        tool_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "athena:GetQueryExecution",
                    "athena:GetQueryResults",
                    "athena:StartQueryExecution",
                ],
                resources=[f"arn:aws:athena:{self.region}:{self.account}:workgroup/*"]
            )
        )

        # MWAA permissions (get_mwaa_logs, retry_airflow_dag)
        tool_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "airflow:GetEnvironment",
                    "airflow:CreateCliToken",
                ],
                resources=[f"arn:aws:airflow:{self.region}:{self.account}:environment/*"]
            )
        )

        # S3 permissions (get_s3_logs, verify_source_data + RCA storage)
        rca_bucket.grant_read_write(tool_lambda_role)
        tool_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3:GetObject",
                    "s3:ListBucket",
                    "s3:GetBucketLocation",
                ],
                resources=["arn:aws:s3:::*"]
            )
        )

        # CloudWatch permissions (get_cloudwatch_alarm, all log fetching)
        tool_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudwatch:DescribeAlarms",
                    "cloudwatch:GetMetricData",
                    "logs:GetLogEvents",
                    "logs:FilterLogEvents",
                    "logs:DescribeLogGroups",
                ],
                resources=["*"]
            )
        )

        # Kafka/MSK permissions (retry_kafka)
        tool_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "lambda:GetEventSourceMapping",
                    "lambda:UpdateEventSourceMapping",
                    "lambda:InvokeFunction",
                ],
                resources=[f"arn:aws:lambda:{self.region}:{self.account}:function:*"]
            )
        )

        # ServiceNow update — needs Secrets Manager access
        servicenow_secret.grant_read(tool_lambda_role)

        # Glue catalog (verify_source_data)
        tool_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "glue:GetTable",
                    "glue:GetPartitions",
                    "glue:GetDatabase",
                ],
                resources=[
                    f"arn:aws:glue:{self.region}:{self.account}:catalog",
                    f"arn:aws:glue:{self.region}:{self.account}:database/*",
                    f"arn:aws:glue:{self.region}:{self.account}:table/*/*",
                ]
            )
        )

        # =================================================================
        # IAM Role for Orchestrator Lambda
        # =================================================================
        orchestrator_role = iam.Role(
            self,
            "OrchestratorLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ]
        )

        # Bedrock model invocation
        orchestrator_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/*",
                    f"arn:aws:bedrock:us-*::foundation-model/*",
                ]
            )
        )

        # AgentCore Gateway access (invoke MCP tools via Gateway)
        orchestrator_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeAgent",
                    "bedrock:Retrieve",
                ],
                resources=["*"]
            )
        )

        # S3 for RCA storage
        rca_bucket.grant_read_write(orchestrator_role)

        # CloudWatch metrics
        orchestrator_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"]
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
        # Tool Lambda Functions (13 tools behind AgentCore Gateway)
        # =================================================================
        # These are invoked by the Gateway, NOT directly by the orchestrator.
        # Architecture: Agent -> MCPClient -> Gateway (IAM) -> Tool Lambda

        tool_lambdas = {}
        tool_definitions = [
            # Investigation tools
            ("get_emr_logs", "Retrieve EMR cluster and step logs", 30),
            ("get_glue_logs", "Retrieve Glue ETL job run logs", 30),
            ("get_mwaa_logs", "Retrieve MWAA Airflow logs", 30),
            ("get_cloudwatch_alarm", "Retrieve CloudWatch alarm details", 30),
            ("get_s3_logs", "Retrieve S3 access logs", 30),
            ("verify_source_data", "Verify source data availability", 30),
            ("get_athena_query", "Retrieve Athena query details", 30),
            # Remediation tools
            ("retry_emr", "Retry failed EMR step", 300),
            ("retry_glue_job", "Retry failed Glue job", 300),
            ("retry_airflow_dag", "Trigger Airflow DAG run", 300),
            ("retry_athena_query", "Retry Athena query", 300),
            ("retry_kafka", "Retry Kafka event processing", 300),
            # ServiceNow integration
            ("update_servicenow_ticket", "Update ServiceNow incident", 30),
        ]

        for tool_name, description, timeout_seconds in tool_definitions:
            fn = lambda_.Function(
                self,
                f"Tool{tool_name.replace('_', ' ').title().replace(' ', '')}",
                function_name=f"incident-handler-{tool_name.replace('_', '-')}",
                runtime=lambda_.Runtime.PYTHON_3_11,
                handler="handler.handler",
                code=lambda_.Code.from_asset(f"../lambdas/{tool_name}"),
                role=tool_lambda_role,
                timeout=Duration.seconds(timeout_seconds),
                memory_size=512,
                environment={
                    "LOG_LEVEL": "INFO",
                    "RCA_BUCKET": rca_bucket.bucket_name,
                },
                layers=[dependencies_layer],
                log_retention=logs.RetentionDays.ONE_MONTH,
                description=description,
            )
            tool_lambdas[tool_name] = fn

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
            role=orchestrator_role,
            timeout=Duration.minutes(15),
            memory_size=1024,
            environment={
                "RCA_BUCKET": rca_bucket.bucket_name,
                "GATEWAY_ENDPOINT": "",  # Set after Gateway is created
                "GATEWAY_REGION": self.region,
                "BEDROCK_MODEL_ID": "us.anthropic.claude-sonnet-4-20250514",
                "LOG_LEVEL": "INFO",
                "METRICS_NAMESPACE": "IncidentHandler",
            },
            layers=[dependencies_layer],
            log_retention=logs.RetentionDays.ONE_MONTH,
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
            role=orchestrator_role,
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "ASSIGNMENT_GROUP": "Data Lake Platform Team",
                "ORCHESTRATOR_LAMBDA": orchestrator_lambda.function_name,
                "POLL_LIMIT": "10",
                "MINUTES_BACK": "10",
                "LOG_LEVEL": "INFO",
            },
            layers=[dependencies_layer],
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        # Poller needs to invoke orchestrator
        orchestrator_lambda.grant_invoke(poller_lambda)

        # =================================================================
        # EventBridge Rule for Polling
        # =================================================================
        polling_rule = events.Rule(
            self,
            "PollingRule",
            rule_name="incident-poller-schedule",
            description="Trigger incident poller every 5 minutes",
            schedule=events.Schedule.rate(Duration.minutes(5)),
            enabled=True,
        )
        polling_rule.add_target(targets.LambdaFunction(poller_lambda))

        # =================================================================
        # Outputs
        # =================================================================
        CfnOutput(self, "RCABucketName",
                  value=rca_bucket.bucket_name,
                  description="S3 bucket for RCA storage")

        CfnOutput(self, "OrchestratorFunctionName",
                  value=orchestrator_lambda.function_name,
                  description="Orchestrator Lambda function name")

        CfnOutput(self, "PollerFunctionName",
                  value=poller_lambda.function_name,
                  description="Poller Lambda function name")

        CfnOutput(self, "SecretARN",
                  value=servicenow_secret.secret_arn,
                  description="ServiceNow credentials secret ARN")

        # Output all tool Lambda ARNs for Gateway configuration
        for tool_name, fn in tool_lambdas.items():
            CfnOutput(self, f"Tool{tool_name.replace('_', '')}ARN",
                      value=fn.function_arn,
                      description=f"Tool Lambda ARN: {tool_name}")

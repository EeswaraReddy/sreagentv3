"""Lambda Stack for all incident handler tools."""
from aws_cdk import (
    Stack,
    Duration,
    aws_lambda as _lambda,
    aws_iam as iam,
    aws_s3 as s3,
    aws_logs as logs,
    CfnOutput,
)
from constructs import Construct


class LambdaStack(Stack):
    """Creates all Lambda functions for incident handler tools."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        rca_bucket: s3.IBucket,
        **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.functions: dict[str, _lambda.Function] = {}
        self.rca_bucket = rca_bucket

        # Common Lambda configuration
        common_props = {
            "runtime": _lambda.Runtime.PYTHON_3_11,
            "timeout": Duration.seconds(30),
            "memory_size": 256,
            "tracing": _lambda.Tracing.ACTIVE,
            "log_retention": logs.RetentionDays.ONE_MONTH,
        }

        # ============ LOG TOOLS ============
        self._create_log_tools(common_props)

        # ============ DATA TOOLS ============
        self._create_data_tools(common_props)

        # ============ RETRY TOOLS ============
        self._create_retry_tools(common_props)

        # ============ SERVICENOW TOOL ============
        self._create_servicenow_tool(common_props)

    def _create_log_tools(self, common_props: dict) -> None:
        """Create log retrieval Lambda functions."""

        # get_emr_logs
        emr_logs_role = self._create_role("GetEmrLogsRole", [
            iam.PolicyStatement(
                actions=["elasticmapreduce:DescribeCluster", "elasticmapreduce:DescribeStep",
                         "elasticmapreduce:ListSteps"],
                resources=["*"],
            ),
            iam.PolicyStatement(
                actions=["logs:GetLogEvents", "logs:FilterLogEvents"],
                resources=["arn:aws:logs:*:*:log-group:/aws/emr/*"],
            ),
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=["arn:aws:s3:::aws-logs-*", "arn:aws:s3:::aws-logs-*/*"],
            ),
        ])
        self.functions["get_emr_logs"] = _lambda.Function(
            self, "GetEmrLogs",
            function_name="incident-handler-get-emr-logs",
            handler="handler.handler",
            code=_lambda.Code.from_asset("../lambdas/get_emr_logs"),
            role=emr_logs_role,
            **common_props
        )

        # get_glue_logs
        glue_logs_role = self._create_role("GetGlueLogsRole", [
            iam.PolicyStatement(
                actions=["glue:GetJobRun", "glue:GetJobRuns", "glue:GetJob"],
                resources=["*"],
            ),
            iam.PolicyStatement(
                actions=["logs:GetLogEvents", "logs:FilterLogEvents"],
                resources=["arn:aws:logs:*:*:log-group:/aws-glue/*"],
            ),
        ])
        self.functions["get_glue_logs"] = _lambda.Function(
            self, "GetGlueLogs",
            function_name="incident-handler-get-glue-logs",
            handler="handler.handler",
            code=_lambda.Code.from_asset("../lambdas/get_glue_logs"),
            role=glue_logs_role,
            **common_props
        )

        # get_mwaa_logs
        mwaa_logs_role = self._create_role("GetMwaaLogsRole", [
            iam.PolicyStatement(
                actions=["airflow:GetEnvironment"],
                resources=["*"],
            ),
            iam.PolicyStatement(
                actions=["logs:GetLogEvents", "logs:FilterLogEvents", "logs:DescribeLogStreams"],
                resources=["arn:aws:logs:*:*:log-group:airflow-*"],
            ),
        ])
        self.functions["get_mwaa_logs"] = _lambda.Function(
            self, "GetMwaaLogs",
            function_name="incident-handler-get-mwaa-logs",
            handler="handler.handler",
            code=_lambda.Code.from_asset("../lambdas/get_mwaa_logs"),
            role=mwaa_logs_role,
            **common_props
        )

        # get_cloudwatch_alarm
        cw_alarm_role = self._create_role("GetCwAlarmRole", [
            iam.PolicyStatement(
                actions=["cloudwatch:DescribeAlarms", "cloudwatch:DescribeAlarmHistory",
                         "cloudwatch:GetMetricData"],
                resources=["*"],
            ),
        ])
        self.functions["get_cloudwatch_alarm"] = _lambda.Function(
            self, "GetCloudWatchAlarm",
            function_name="incident-handler-get-cloudwatch-alarm",
            handler="handler.handler",
            code=_lambda.Code.from_asset("../lambdas/get_cloudwatch_alarm"),
            role=cw_alarm_role,
            **common_props
        )

        # get_athena_query
        athena_query_role = self._create_role("GetAthenaQueryRole", [
            iam.PolicyStatement(
                actions=["athena:GetQueryExecution", "athena:GetQueryResults"],
                resources=["*"],
            ),
        ])
        self.functions["get_athena_query"] = _lambda.Function(
            self, "GetAthenaQuery",
            function_name="incident-handler-get-athena-query",
            handler="handler.handler",
            code=_lambda.Code.from_asset("../lambdas/get_athena_query"),
            role=athena_query_role,
            **common_props
        )

    def _create_data_tools(self, common_props: dict) -> None:
        """Create data validation Lambda functions."""

        # get_s3_logs
        s3_logs_role = self._create_role("GetS3LogsRole", [
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=["*"],
            ),
        ])
        self.functions["get_s3_logs"] = _lambda.Function(
            self, "GetS3Logs",
            function_name="incident-handler-get-s3-logs",
            handler="handler.handler",
            code=_lambda.Code.from_asset("../lambdas/get_s3_logs"),
            role=s3_logs_role,
            **common_props
        )

        # verify_source_data
        verify_data_role = self._create_role("VerifySourceDataRole", [
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:ListBucket", "s3:HeadObject"],
                resources=["*"],
            ),
            iam.PolicyStatement(
                actions=["glue:GetTable", "glue:GetPartitions"],
                resources=["*"],
            ),
        ])
        self.functions["verify_source_data"] = _lambda.Function(
            self, "VerifySourceData",
            function_name="incident-handler-verify-source-data",
            handler="handler.handler",
            code=_lambda.Code.from_asset("../lambdas/verify_source_data"),
            role=verify_data_role,
            **common_props
        )

    def _create_retry_tools(self, common_props: dict) -> None:
        """Create retry action Lambda functions."""

        # retry_emr
        retry_emr_role = self._create_role("RetryEmrRole", [
            iam.PolicyStatement(
                actions=["elasticmapreduce:AddJobFlowSteps", "elasticmapreduce:DescribeStep"],
                resources=["*"],
            ),
        ])
        self.functions["retry_emr"] = _lambda.Function(
            self, "RetryEmr",
            function_name="incident-handler-retry-emr",
            handler="handler.handler",
            code=_lambda.Code.from_asset("../lambdas/retry_emr"),
            role=retry_emr_role,
            timeout=Duration.minutes(5),
            **{k: v for k, v in common_props.items() if k != "timeout"}
        )

        # retry_glue_job
        retry_glue_role = self._create_role("RetryGlueJobRole", [
            iam.PolicyStatement(
                actions=["glue:StartJobRun", "glue:GetJobRun"],
                resources=["*"],
            ),
        ])
        self.functions["retry_glue_job"] = _lambda.Function(
            self, "RetryGlueJob",
            function_name="incident-handler-retry-glue-job",
            handler="handler.handler",
            code=_lambda.Code.from_asset("../lambdas/retry_glue_job"),
            role=retry_glue_role,
            timeout=Duration.minutes(5),
            **{k: v for k, v in common_props.items() if k != "timeout"}
        )

        # retry_airflow_dag
        retry_dag_role = self._create_role("RetryAirflowDagRole", [
            iam.PolicyStatement(
                actions=["airflow:CreateCliToken"],
                resources=["*"],
            ),
        ])
        self.functions["retry_airflow_dag"] = _lambda.Function(
            self, "RetryAirflowDag",
            function_name="incident-handler-retry-airflow-dag",
            handler="handler.handler",
            code=_lambda.Code.from_asset("../lambdas/retry_airflow_dag"),
            role=retry_dag_role,
            timeout=Duration.minutes(5),
            **{k: v for k, v in common_props.items() if k != "timeout"}
        )

        # retry_athena_query
        retry_athena_role = self._create_role("RetryAthenaQueryRole", [
            iam.PolicyStatement(
                actions=["athena:StartQueryExecution", "athena:GetQueryExecution"],
                resources=["*"],
            ),
            iam.PolicyStatement(
                actions=["s3:PutObject", "s3:GetObject"],
                resources=["arn:aws:s3:::*-athena-results/*"],
            ),
        ])
        self.functions["retry_athena_query"] = _lambda.Function(
            self, "RetryAthenaQuery",
            function_name="incident-handler-retry-athena-query",
            handler="handler.handler",
            code=_lambda.Code.from_asset("../lambdas/retry_athena_query"),
            role=retry_athena_role,
            timeout=Duration.minutes(5),
            **{k: v for k, v in common_props.items() if k != "timeout"}
        )

        # retry_kafka
        retry_kafka_role = self._create_role("RetryKafkaRole", [
            iam.PolicyStatement(
                actions=["kafka:DescribeCluster", "kafka:GetBootstrapBrokers"],
                resources=["*"],
            ),
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=["arn:aws:lambda:*:*:function:*kafka*"],
            ),
        ])
        self.functions["retry_kafka"] = _lambda.Function(
            self, "RetryKafka",
            function_name="incident-handler-retry-kafka",
            handler="handler.handler",
            code=_lambda.Code.from_asset("../lambdas/retry_kafka"),
            role=retry_kafka_role,
            timeout=Duration.minutes(5),
            **{k: v for k, v in common_props.items() if k != "timeout"}
        )

    def _create_servicenow_tool(self, common_props: dict) -> None:
        """Create ServiceNow integration Lambda."""

        servicenow_role = self._create_role("UpdateServiceNowRole", [
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:servicenow/*"],
            ),
        ])

        # Grant write access to RCA bucket
        self.rca_bucket.grant_write(servicenow_role)

        self.functions["update_servicenow_ticket"] = _lambda.Function(
            self, "UpdateServiceNowTicket",
            function_name="incident-handler-update-servicenow-ticket",
            handler="handler.handler",
            code=_lambda.Code.from_asset("../lambdas/update_servicenow_ticket"),
            role=servicenow_role,
            environment={
                "RCA_BUCKET": self.rca_bucket.bucket_name,
                "RCA_PREFIX": "rca/",
                "SERVICENOW_SECRET_ARN": f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:servicenow/oauth",
            },
            **common_props
        )

        # Output function ARNs
        for name, func in self.functions.items():
            CfnOutput(self, f"{name}Arn", value=func.function_arn)

    def _create_role(self, role_name: str, statements: list) -> iam.Role:
        """Helper to create IAM role with common permissions."""
        role = iam.Role(
            self, role_name,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )

        # Add CloudWatch Logs permissions (common to all Lambdas)
        role.add_to_policy(iam.PolicyStatement(
            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            resources=["*"],
        ))

        # Add X-Ray tracing permissions
        role.add_to_policy(iam.PolicyStatement(
            actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
            resources=["*"],
        ))

        # Add custom statements
        for stmt in statements:
            role.add_to_policy(stmt)

        return role

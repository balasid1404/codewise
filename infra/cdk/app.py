#!/usr/bin/env python3
"""CDK app for Fault Localization infrastructure."""

import aws_cdk as cdk
from stack import FaultLocalizationStack

app = cdk.App()

FaultLocalizationStack(
    app,
    "FaultLocalizationStack",
    env=cdk.Environment(
        account=cdk.Aws.ACCOUNT_ID,
        region="us-east-1"
    )
)

app.synth()

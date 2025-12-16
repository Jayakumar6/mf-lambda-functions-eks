# EKS Node Group Patching Automation Lambda (Hub-and-Spoke Pattern)

This Lambda function automates the process of patching a **specific EKS Cluster** in a **specific Target AWS Account** using a centralized Golden Image AMI from the Hub Account. It follows a "Fire-and-Forget" pattern, triggering the update on AWS and exiting immediately, allowing external tools (like Jenkins) to handle long-running monitoring.

## 🚀 Architecture

*   **Hub Account (Central)**: Hosts the Lambda and the Golden AMI ID (in Parameter Store).
*   **Spoke Accounts (Targets)**: Host the EKS Clusters and Node Groups.
*   **Workflow**:
    1.  Jenkins triggers Lambda with `account_id`, `cluster_name`, and `sns_topic_arn`.
    2.  Lambda fetches the Golden AMI ID from the Hub Account's Parameter Store.
    3.  Lambda assumes the `CrossAccount-EKS-Patcher-Role` in the Spoke Account.
    4.  **Idempotency Check**:
        *   If Node Group is `UPDATING`: Skips trigger and logs warning.
        *   If Node Group is already on Target AMI: Skips trigger and logs success.
    5.  **Trigger Update**: Lambda creates a new Launch Template version and updates the Node Group.
    6.  **Fire-and-Forget**: Lambda logs the Update ID and exits immediately (does not wait for completion).
    7.  **Notification**: Lambda sends an SNS notification confirming the update trigger.
    8.  **Monitoring**: Jenkins Pipeline (Stage 4) monitors the update until completion.

## 📋 Prerequisites

*   **Runtime**: Python 3.9+
*   **Timeout**: Set to **15 minutes** (900 seconds) - *Though it finishes in seconds, keeping buffer is good.*
*   **Memory**: 128 MB.

## ⚙️ Environment Variables

Configure the following environment variables for the Lambda function:

| Variable Name     | Description                                      | Example |
| ----------------- | ------------------------------------------------ | ------- |
| `PARAMETER_NAME`  | SSM Parameter path for the Golden AMI ID (in Hub Account) | `eks-golden-image` |
| `SNS_TOPIC_ARN`   | (Optional) Default ARN of the SNS Topic for notifications | `arn:aws:sns:ap-south-1:123456789012:eks-patch` |
| `TARGET_ROLE_NAME`| Name of the Cross-Account Role to assume         | `CrossAccount-EKS-Patcher-Role` |

**Note**: `cluster_name`, `account_id`, and `sns_topic_arn` can be passed dynamically via the event payload.

## 📦 Deployment

### 1. Target Accounts (Spokes)
Deploy the `target_account_role.yaml` CloudFormation template (or create IAM role) in **every** target account.
*   **Role Name**: `CrossAccount-EKS-Patcher-Role`
*   **Trust Policy**: Allow Hub Account ID (`047861164910`) to AssumeRole.
*   **Permissions**: `eks:*`, `ec2:CreateLaunchTemplateVersion`, `ec2:Describe*`, `iam:PassRole`.

### 2. Common Account (Hub)
Deploy the Lambda function as described below.

#### AWS CLI Deployment

1.  **Create IAM Role**:
```bash
# Create trust policy
cat > trust-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "lambda.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

# Create role
aws iam create-role \
    --role-name eks-patching-lambda-role \
    --assume-role-policy-document file://trust-policy.json
```

2.  **Attach Policies**:
    *   **Cross-Account**: Allow `sts:AssumeRole` on `arn:aws:iam::*:role/CrossAccount-EKS-Patcher-Role`.
    *   **SSM**: Allow `ssm:GetParameter` on `eks-golden-image` (Local).
    *   **SNS**: Allow `sns:Publish` (Local).
    *   **Logs**: Allow `logs:*`.

3.  **Package Code**:
```bash
zip patching_function.zip eks_patching_lambda.py
```

4.  **Create/Update Lambda Function**:
```bash
aws lambda update-function-code \
    --function-name EKS-NodeGroup-Patcher \
    --zip-file fileb://patching_function.zip
```

## 🧪 Jenkins Integration

The Lambda expects the following JSON payload:

```json
{
  "account_id": "779527285137",
  "cluster_name": "terraform-eks-cluster",
  "sns_topic_arn": "arn:aws:sns:ap-south-1:047861164910:eks-patch"
}
```

### Jenkins Pipeline Example (Groovy)

```groovy
stage('Trigger EKS Patching') {
    steps {
        script {
            def payload = """
            {
                "account_id": "${params.TARGET_ACCOUNT_ID}",
                "cluster_name": "${params.CLUSTER_NAME}",
                "sns_topic_arn": "${params.SNS_TOPIC_ARN}"
            }
            """
            // Hub Profile triggers Lambda
            withEnv(["AWS_PROFILE=${HUB_PROFILE}"]) {
                sh "aws lambda invoke --function-name EKS-NodeGroup-Patcher --payload '${payload}' response.json"
            }
        }
    }
}
stage('Monitor Update') {
    steps {
        script {
           // Target Profile monitors EKS
           withEnv(["AWS_PROFILE=${TARGET_PROFILE}"]) {
               sh "python3 -u monitor_eks_update.py ..."
           }
        }
    }
}
```

## 🔐 IAM Permissions Detail

### Hub Account (Lambda Role)
```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "sts:AssumeRole",
            "Resource": "arn:aws:iam::*:role/CrossAccount-EKS-Patcher-Role"
        },
        {
            "Effect": "Allow",
            "Action": "ssm:GetParameter",
            "Resource": "*"
        },
        {
            "Effect": "Allow",
            "Action": "sns:Publish",
            "Resource": "*"
        }
    ]
}
```

### Spoke Accounts (Cross-Account Role)
```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "eks:DescribeNodegroup",
                "eks:ListNodegroups",
                "eks:UpdateNodegroupVersion",
                "eks:DescribeUpdate",
                "eks:ListUpdates"
            ],
            "Resource": "*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "ec2:DescribeLaunchTemplates",
                "ec2:DescribeLaunchTemplateVersions",
                "ec2:CreateLaunchTemplateVersion"
            ],
            "Resource": "*"
        },
        {
            "Effect": "Allow",
            "Action": "iam:PassRole",
            "Resource": "*"
        }
    ]
}
```

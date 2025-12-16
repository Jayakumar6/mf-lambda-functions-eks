# AMI Creator Lambda Function

This Lambda function automates the creation of Golden Image AMIs from vulnerability-free EC2 instances. It supports **Cross-Region Replication (DR)** and **Cross-Account Sharing**, ensuring the latest AMI is available and referenced in the SSM Parameter Store across your entire AWS organization.

## 🚀 Workflow

1.  **Input**: Receives EC2 Instance ID (patched/golden instance).
2.  **Create AMI (Member/Source Region)**: Creates an AMI in the source region (e.g., `ap-south-1`).
3.  **Wait**: Monitors AMI creation until it's `AVAILABLE`.
4.  **Share (Source)**: Shares the AMI with specified target AWS accounts.
5.  **Update SSM (Source)**: Updates the SSM Parameter (e.g., `eks-golden-image`) with the new AMI ID.
6.  **Update SSM (Targets)**: Assumes a role in each target account to update their SSM Parameter Store with the new AMI ID.
7.  **Replicate to DR Region**: Copies the AMI to the DR region (e.g., `ap-south-2`).
8.  **Wait (DR)**: Monitors the DR AMI until it's `AVAILABLE`.
9.  **Share (DR)**: Shares the DR AMI with target accounts.
10. **Update SSM (DR Source & Targets)**: Updates SSM Parameter Store in both Source and Target accounts in the DR region.
11. **Notify**: Sends an SNS notification with details from both regions.

## 📋 Prerequisites

*   **Runtime**: Python 3.9+
*   **Timeout**: Set to **15 minutes** (900 seconds) due to AMI copy wait times.
*   **Memory**: 128 MB.

## ⚙️ Input Parameters

### Required

| Parameter | Description | Example |
|-----------|-------------|---------|
| `instance_id` | EC2 Instance ID to create AMI from | `i-0123456789abcdef0` |
| `parameter_name` | SSM Parameter name to update | `eks-golden-image` |

### Optional

| Parameter | Description | Default | Example |
|-----------|-------------|---------|---------|
| `ami_name` | Name for the created AMI | `golden-image-{timestamp}` | `eks-node-v1.32-patched` |
| `ami_description` | Description for the AMI | `Golden Image created by Lambda` | `EKS 1.32 with security patches` |
| `share_accounts` | Comma-separated AWS account IDs | None | `123456789012,987654321098` |
| `target_role_name`| Role to assume in target accounts | `CrossAccount-EKS-Patcher-Role` | `CrossAccount-SSM-Updater` |
| `sns_topic_arn` | SNS topic ARN for notifications | None | `arn:aws:sns:ap-south-1:123:notify` |

## 🔐 IAM Permissions

### 1. Source Account (Lambda Role)

The Lambda needs permissions to manage AMIs, update SSM, and assume roles in target accounts.

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "ec2:CreateImage",
                "ec2:DescribeImages",
                "ec2:DescribeInstances",
                "ec2:ModifyImageAttribute",
                "ec2:CopyImage",
                "ec2:CreateTags",
                "ssm:PutParameter",
                "ssm:GetParameter",
                "sns:Publish",
                "sts:AssumeRole",
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents"
            ],
            "Resource": "*"
        }
    ]
}
```

### 2. Target Accounts (Cross-Account Role)

Target accounts must have a role (e.g., `CrossAccount-EKS-Patcher-Role`) trusting the Source Account, with permissions to update SSM.

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "ssm:PutParameter",
                "ssm:GetParameter"
            ],
            "Resource": "*"
        }
    ]
}
```

## 📦 Deployment

### AWS CLI Deployment

1.  **Create IAM Role & Policy** (Source Account):
    *   Create role `ami-creator-lambda-role` trusting `lambda.amazonaws.com`.
    *   Attach the policy json above.

2.  **Deploy Lambda**:
    ```bash
    zip ami-creator.zip ami_creator_lambda.py
    
    aws lambda create-function \
        --function-name AMI-Creator-Function \
        --runtime python3.9 \
        --role arn:aws:iam::SOURCE_ACCOUNT_ID:role/ami-creator-lambda-role \
        --handler ami_creator_lambda.lambda_handler \
        --zip-file fileb://ami-creator.zip \
        --timeout 900
    ```

## 🤖 Jenkins Pipeline Integration

The recommended way to run this is via Jenkins.

### Pipeline Logic (`Jenkinsfile_amicreation`)
1.  **Invoke Lambda (Async)**: Triggers the Lambda and returns immediately (`Event` invocation type) to avoid timeout errors in Jenkins/CLI.
2.  **Monitor Mumbai AMI**: Jenkins polls the Source Region until the AMI with `AMI_NAME` becomes `AVAILABLE`.
3.  **Monitor Hyderabad AMI**: Jenkins polls the DR Region (`ap-south-2`) until the replicated AMI becomes `AVAILABLE`.

### Example Jenkinsfile Stage
```groovy
stage('Invoke Lambda') {
    steps {
        sh "aws lambda invoke --function-name AMI-Creator-Function --invocation-type Event --payload ... response.json"
    }
}
stage('Monitor AMI') {
    steps {
        // Loop and check aws ec2 describe-images --filters "Name=name,Values=${AMI_NAME}"
    }
}
```

## 📧 Notifications
The Lambda sends a comprehensive SNS notification:
```
✅ Golden Image AMI Created Successfully!

Source (Mumbai): ami-0123...
DR (Hyderabad): ami-0456...
AMI Name: eks-golden-image-v6
Shared with: ['123456789', '987654321']
SSM Updated in Source & Targets.
```

## ⚠️ Important Notes
1.  **Timeout**: Cross-region copying takes time. The Lambda timeout is 15 mins. If the image is very large, it might timeout, but the copy continues in AWS background.
2.  **Permissions**: Ensure the Source Lambda Role has perms for **both regions** (`ap-south-1` and `ap-south-2`).
3.  **Parameter Store**: The same parameter (`eks-golden-image`) is updated in all accounts and regions to ensure consistency for downstream patching automation.

## 📄 Reference: IAM Policy JSONs

### 1. Lambda Trust Policy ()
Use this for the `AMI-Creator-Role` in the Source Account.

```json
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
```

### 2. Lambda Permissions Policy ()
Attached to `AMI-Creator-Role` in the Source Account.

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "ec2:CreateImage",
                "ec2:DescribeImages",
                "ec2:ModifyImageAttribute",
                "ec2:DeregisterImage",
                "ec2:DescribeInstances",
                "ec2:CopyImage",
                "ec2:CreateTags"
            ],
            "Resource": "*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "ssm:PutParameter",
                "ssm:GetParameter"
            ],
            "Resource": "*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "sns:Publish"
            ],
            "Resource": "*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "sts:AssumeRole"
            ],
            "Resource": "*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents"
            ],
            "Resource": "arn:aws:logs:*:*:*"
        }
    ]
}
```

### 3. Target Role Trust Policy ()
Use this for `CrossAccount-EKS-Patcher-Role` in Target Accounts. Replace `SOURCE_ACCOUNT_ID` with your Source Account ID.

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "AWS": "arn:aws:iam::SOURCE_ACCOUNT_ID:root"
            },
            "Action": "sts:AssumeRole"
        }
    ]
}
```

### 4. Target Role Permissions Policy ()
Attached to `CrossAccount-EKS-Patcher-Role` in Target Accounts.

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "eks:ListNodegroups",
                "eks:DescribeNodegroup",
                "eks:UpdateNodegroupVersion",
                "eks:DescribeUpdate",
                "eks:ListClusters",
                "eks:DescribeCluster",
                "ec2:CreateLaunchTemplateVersion",
                "ec2:DescribeLaunchTemplates",
                "ec2:DescribeLaunchTemplateVersions",
                "ec2:RunInstances",
                "ec2:DescribeImages",
                "ec2:DescribeInstances",
                "ec2:DescribeSubnets",
                "ec2:DescribeSecurityGroups",
                "ec2:DescribeKeyPairs",
                "ec2:DescribeNetworkInterfaces",
                "ec2:CreateTags",
                "ssm:GetParameter",
                "ssm:PutParameter",
                "iam:PassRole"
            ],
            "Resource": "*"
        }
    ]
}
```



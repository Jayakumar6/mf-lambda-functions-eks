import boto3
import os
import time
import logging
import json
from botocore.exceptions import ClientError

# Configure Logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def get_clients(account_id=None, role_name=None, region='ap-south-1'):
    try:
        current_account_id = boto3.client('sts').get_caller_identity()['Account']
    except Exception as e:
        logger.warning(f"Could not determine current account ID: {e}")
        current_account_id = None

    if account_id and role_name and account_id != current_account_id:
        sts_client = boto3.client('sts')
        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        try:
            logger.info(f"Assuming role: {role_arn}")
            assumed_role = sts_client.assume_role(
                RoleArn=role_arn,
                RoleSessionName="EKSPatchingSession"
            )
            credentials = assumed_role['Credentials']
            
            eks = boto3.client('eks', region_name=region, aws_access_key_id=credentials['AccessKeyId'], aws_secret_access_key=credentials['SecretAccessKey'], aws_session_token=credentials['SessionToken'])
            ec2 = boto3.client('ec2', region_name=region, aws_access_key_id=credentials['AccessKeyId'], aws_secret_access_key=credentials['SecretAccessKey'], aws_session_token=credentials['SessionToken'])
            ssm = boto3.client('ssm', region_name=region, aws_access_key_id=credentials['AccessKeyId'], aws_secret_access_key=credentials['SecretAccessKey'], aws_session_token=credentials['SessionToken'])
            return eks, ec2, ssm
        except ClientError as e:
            logger.error(f"Failed to assume role {role_arn}: {e}")
            raise
    else:
        logger.info("Using Local Lambda Credentials (Same Account)")
        return boto3.client('eks', region_name=region), boto3.client('ec2', region_name=region), boto3.client('ssm', region_name=region)

def get_latest_ami_id(ssm_client, parameter_name):
    try:
        response = ssm_client.get_parameter(Name=parameter_name)
        ami_id = response['Parameter']['Value']
        logger.info(f"Fetched AMI ID from {parameter_name}: {ami_id}")
        return ami_id
    except ClientError as e:
        logger.error(f"Error fetching AMI ID from {parameter_name}: {e}")
        raise

# ... (Previous helper functions remain unchanged: get_active_node_groups, create_launch_template_version, update_node_group, monitor_update, get_inprogress_update, send_notification)

def process_cluster(account_id, role_name, cluster_name, region):
    results = []
    try:
        eks, ec2, ssm = get_clients(account_id, role_name, region)
        
        # 1. Fetch AMI from Target Account's Cluster-Specific Parameter
        cluster_param_name = f"eks-ami/{cluster_name}"
        try:
            new_ami_id = get_latest_ami_id(ssm, cluster_param_name)
        except Exception as e:
            msg = f"❌ Failed to fetch AMI from target parameter {cluster_param_name}: {e}"
            logger.error(msg)
            return [msg]

        node_groups = get_active_node_groups(eks, cluster_name)
        if not node_groups: return [f"No active node groups found in {cluster_name}"]
        
        for ng_name in node_groups:
            logger.info(f"Processing {ng_name} with AMI {new_ami_id}")
            try:
                # 2. Check Status
                ng_details = eks.describe_nodegroup(clusterName=cluster_name, nodegroupName=ng_name)
                ng_status = ng_details['nodegroup']['status']
                
                if ng_status == 'UPDATING':
                    msg = f"⚠️ {ng_name} is already UPDATING. Skipping trigger."
                    logger.info(msg)
                    results.append(msg)
                    continue
                
                if ng_status != 'ACTIVE':
                    msg = f"⚠️ Skipped {ng_name}: Status is {ng_status} (Must be ACTIVE)"
                    logger.warning(msg)
                    results.append(msg)
                    continue

                # 3. Check current AMI ID (Idempotency)
                lt_config = ng_details['nodegroup']['launchTemplate']
                lt_id = lt_config['id']
                current_ver = lt_config['version']
                
                try:
                    # Resolve $Latest if needed, or stick to explicit version logic
                    # To be safe, we assume version number. If $Latest is used, we might need to resolve it.
                    # Terraform Sync uses $Latest, but EKS API reports the integer version it currently uses.
                    
                    resp = ec2.describe_launch_template_versions(LaunchTemplateId=lt_id, Versions=[str(current_ver)])
                    current_lt_data = resp['LaunchTemplateVersions'][0]['LaunchTemplateData']
                    current_ami_id = current_lt_data.get('ImageId')
                    
                    if current_ami_id == new_ami_id:
                         msg = f"✅ {ng_name}: Already up-to-date with AMI {new_ami_id}. Skipping."
                         logger.info(msg)
                         results.append(msg)
                         continue
                except Exception as e:
                    logger.warning(f"Could not verify current AMI ID: {e}")

                # 4. Create LT Version & Update
                new_ver = create_launch_template_version(ec2, lt_id, current_ver, new_ami_id)
                update_id = update_node_group(eks, cluster_name, ng_name, lt_id, new_ver)
                
                # Fire and Forget - Do NOT monitor
                msg = f"🚀 {ng_name}: Update Triggered (Update ID: {update_id})"
                logger.info(msg)
                results.append(msg)
                
            except Exception as e:
                logger.error(f"Error processing {ng_name}: {e}")
                results.append(f"❌ {ng_name}: Error - {e}")
    except Exception as e:
        logger.error(f"Critical Error: {e}")
        results.append(f"❌ Error: {e}")
    return results

def lambda_handler(event, context):
    logger.info(f"Event: {json.dumps(event)}")
    target_account_id = event.get('account_id')
    cluster_name = event.get('cluster_name')
    sns_topic_arn = event.get('sns_topic_arn') or os.environ.get('SNS_TOPIC_ARN')
    target_role_name = os.environ.get('TARGET_ROLE_NAME', 'CrossAccount-EKS-Patcher-Role')
    region = os.environ.get('AWS_REGION', 'ap-south-1')
    
    if not target_account_id or not cluster_name: raise ValueError("Missing required inputs: account_id or cluster_name")
    
    # Process Cluster (Fetches AMI internally from Target Account)
    results = process_cluster(target_account_id, target_role_name, cluster_name, region)
    
    format_results = "\n".join(results)
    send_notification(sns_topic_arn, f"EKS Patching: {cluster_name}", f"Results:\n\n{format_results}")
    
    return {"statusCode": 200, "body": json.dumps({"account": target_account_id, "cluster": cluster_name, "results": results})}

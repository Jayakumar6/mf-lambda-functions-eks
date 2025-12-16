import boto3
import os
import time
import logging
import json

# Configure Logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS Clients (Global clients are for default region)
ec2_client = boto3.client('ec2')
ssm_client = boto3.client('ssm')
sns_client = boto3.client('sns')

def create_ami_from_instance(instance_id, ami_name, ami_description):
    """Creates an AMI from the specified EC2 instance."""
    try:
        response = ec2_client.create_image(
            InstanceId=instance_id,
            Name=ami_name,
            Description=ami_description,
            NoReboot=True
        )
        ami_id = response['ImageId']
        logger.info(f"AMI creation initiated: {ami_id} from instance {instance_id}")
        return ami_id
    except Exception as e:
        logger.error(f"Error creating AMI: {e}")
        raise

def wait_for_ami_available(ami_id, region=None, timeout_seconds=1800):
    """Waits for the AMI to become available in a specific region."""
    client = boto3.client('ec2', region_name=region) if region else ec2_client
    logger.info(f"Waiting for AMI {ami_id} to become available in {region or 'default'}...")
    
    start_time = time.time()
    
    while True:
        if (time.time() - start_time) > timeout_seconds:
            logger.warning(f"Timeout waiting for AMI {ami_id}.")
            return "TIMEOUT"
        
        try:
            response = client.describe_images(ImageIds=[ami_id])
            if not response['Images']:
                logger.error(f"AMI {ami_id} not found!")
                return "NOT_FOUND"
            
            state = response['Images'][0]['State']
            if state == 'available':
                logger.info(f"AMI {ami_id} is now available!")
                return "AVAILABLE"
            elif state == 'failed':
                logger.error(f"AMI {ami_id} creation failed!")
                return "FAILED"
            
            time.sleep(30)
            
        except Exception as e:
            logger.error(f"Error checking AMI status: {e}")
            raise

def share_ami_with_accounts(ami_id, account_ids, region=None):
    """Shares the AMI with specified AWS account IDs in a specific region."""
    if not account_ids:
        return
    
    client = boto3.client('ec2', region_name=region) if region else ec2_client
    try:
        client.modify_image_attribute(
            ImageId=ami_id,
            LaunchPermission={
                'Add': [{'UserId': account_id} for account_id in account_ids]
            }
        )
        logger.info(f"AMI {ami_id} shared with accounts: {account_ids} in {region or 'default'}")
    except Exception as e:
        logger.error(f"Error sharing AMI: {e}")
        raise

def update_parameter_store(parameter_name, ami_id, description, region=None):
    """Updates the SSM Parameter Store with the new AMI ID in a specific region (Source Account)."""
    client = boto3.client('ssm', region_name=region) if region else ssm_client
    try:
        client.put_parameter(
            Name=parameter_name,
            Value=ami_id,
            Description=description,
            Type='String',
            Overwrite=True
        )
        logger.info(f"Parameter Store updated: {parameter_name} = {ami_id} in {region or 'default'}")
    except Exception as e:
        logger.error(f"Error updating Parameter Store: {e}")
        raise

def update_ssm_in_target_accounts(account_ids, role_name, parameter_name, ami_id, description, region=None):
    """Updates SSM Parameter Store in all target accounts by assuming a role."""
    if not account_ids or not role_name:
        return
        
    sts = boto3.client('sts')
    
    for acc_id in account_ids:
        logger.info(f"Updating SSM in target account {acc_id} region {region or 'default'}...")
        try:
            role_arn = f"arn:aws:iam::{acc_id}:role/{role_name}"
            # Assume role
            creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="AMICreatorUpdateSSM")['Credentials']
            
            # Create SSM client with assumed credentials
            ssm_target = boto3.client(
                'ssm',
                region_name=region,
                aws_access_key_id=creds['AccessKeyId'],
                aws_secret_access_key=creds['SecretAccessKey'],
                aws_session_token=creds['SessionToken']
            )
            
            ssm_target.put_parameter(
                Name=parameter_name,
                Value=ami_id,
                Description=description,
                Type='String',
                Overwrite=True
            )
            logger.info(f"Successfully updated {parameter_name} in account {acc_id} ({region or 'default'})")
        except Exception as e:
            logger.error(f"Failed to update SSM in account {acc_id}: {e}")

def copy_ami_to_region(source_ami_id, source_region, dest_region, ami_name, ami_description):
    """Copies an AMI to another region."""
    logger.info(f"Copying AMI {source_ami_id} from {source_region} to {dest_region}...")
    dest_ec2 = boto3.client('ec2', region_name=dest_region)
    try:
        response = dest_ec2.copy_image(
            SourceImageId=source_ami_id,
            SourceRegion=source_region,
            Name=ami_name,
            Description=ami_description
        )
        new_ami_id = response['ImageId']
        logger.info(f"Copy initiated. New AMI ID: {new_ami_id}")
        return new_ami_id
    except Exception as e:
        logger.error(f"Error copying AMI: {e}")
        raise

def send_notification(topic_arn, subject, message):
    if not topic_arn: return
    try:
        sns_client.publish(TopicArn=topic_arn, Subject=subject, Message=message)
    except Exception as e:
        logger.error(f"Error sending notification: {e}")

def lambda_handler(event, context):
    logger.info("Starting AMI Creation Process")
    
    instance_id = event.get('instance_id', os.environ.get('INSTANCE_ID'))
    parameter_name = event.get('parameter_name', os.environ.get('PARAMETER_NAME'))
    ami_name = event.get('ami_name', os.environ.get('AMI_NAME', f"golden-image-{int(time.time())}"))
    ami_description = event.get('ami_description', os.environ.get('AMI_DESCRIPTION', 'Golden Image created by Lambda'))
    share_accounts_str = event.get('share_accounts', os.environ.get('SHARE_ACCOUNTS', ''))
    sns_topic_arn = event.get('sns_topic_arn', os.environ.get('SNS_TOPIC_ARN'))
    target_role_name = event.get('target_role_name', os.environ.get('TARGET_ROLE_NAME', 'CrossAccount-EKS-Patcher-Role'))
    
    share_accounts = [acc.strip() for acc in share_accounts_str.split(',') if acc.strip()] if share_accounts_str else []
    
    if not all([instance_id, parameter_name]):
        raise ValueError("Missing required parameters")
    
    try:
        # 1. Create AMI in Primary Region (ap-south-1)
        ami_id = create_ami_from_instance(instance_id, ami_name, ami_description)
        
        # 2. Wait for Primary AMI
        status = wait_for_ami_available(ami_id)
        if status != "AVAILABLE":
            raise Exception(f"Primary AMI creation failed: {status}")
            
        # 3. Share Primary AMI
        if share_accounts:
            share_ami_with_accounts(ami_id, share_accounts)
            
        # 4. Update Primary SSM (Source Account)
        update_parameter_store(parameter_name, ami_id, f"Golden Image AMI - {ami_name}")
        
        # 5. Update Target SSMs (Primary Region)
        if share_accounts:
            update_ssm_in_target_accounts(share_accounts, target_role_name, parameter_name, ami_id, f"Golden Image AMI - {ami_name}")
        
        # --- DR Region (Hyderabad) Logic ---
        dr_region = 'ap-south-2'
        source_region = 'ap-south-1'
        dr_ami_id = None
        
        try:
            # 6. Copy to Hyderabad
            dr_ami_id = copy_ami_to_region(ami_id, source_region, dr_region, ami_name, ami_description)
            
            # 7. Wait for DR AMI
            dr_status = wait_for_ami_available(dr_ami_id, region=dr_region)
            
            if dr_status == "AVAILABLE":
                # 8. Share DR AMI
                if share_accounts:
                    share_ami_with_accounts(dr_ami_id, share_accounts, region=dr_region)
                
                # 9. Update DR SSM (Source Account)
                update_parameter_store(parameter_name, dr_ami_id, f"Golden Image AMI - {ami_name}", region=dr_region)
                
                # 10. Update DR SSM (Target Accounts)
                if share_accounts:
                    update_ssm_in_target_accounts(share_accounts, target_role_name, parameter_name, dr_ami_id, f"Golden Image AMI - {ami_name}", region=dr_region)

            else:
                logger.error(f"DR AMI copy failed status: {dr_status}")
                
        except Exception as e:
            logger.error(f"Error in DR replication: {e}")
            # We don't fail the whole process if DR fails, but we log it
        
        # Notification
        success_msg = (f"✅ Golden Image AMI Created Successfully!\n\n"
                      f"Source (Mumbai): {ami_id}\n"
                      f"DR (Hyderabad): {dr_ami_id if dr_ami_id else 'Failed/Skipped'}\n"
                      f"AMI Name: {ami_name}\n"
                      f"Shared with: {share_accounts}\n"
                      f"SSM Updated in Source & Targets.")
        
        if sns_topic_arn:
            send_notification(sns_topic_arn, "AMI Creation Success", success_msg)
            
        return {
            "statusCode": 200,
            "body": json.dumps({
                "ami_id": ami_id,
                "dr_ami_id": dr_ami_id,
                "ami_name": ami_name
            })
        }
        
    except Exception as e:
        logger.error(f"Critical Error: {e}")
        if sns_topic_arn:
            send_notification(sns_topic_arn, "AMI Creation Error", str(e))
        raise

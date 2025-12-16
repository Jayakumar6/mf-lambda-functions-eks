import boto3
import time
import sys
import subprocess
import json
import os

def get_asg_name(cluster_name, nodegroup_name, region):
    eks = boto3.client('eks', region_name=region)
    resp = eks.describe_nodegroup(clusterName=cluster_name, nodegroupName=nodegroup_name)
    # ASG is in resources -> autoScalingGroups
    asgs = resp['nodegroup']['resources']['autoScalingGroups']
    if not asgs:
        print(f"No ASG found for nodegroup {nodegroup_name}")
        sys.exit(1)
    return asgs[0]['name']

def get_asg_details(asg_name, region):
    asg_client = boto3.client('autoscaling', region_name=region)
    resp = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
    return resp['AutoScalingGroups'][0]

def get_instance_details(instance_ids, region):
    if not instance_ids:
        return {}
    ec2 = boto3.client('ec2', region_name=region)
    resp = ec2.describe_instances(InstanceIds=instance_ids)
    instances = {}
    for r in resp['Reservations']:
        for i in r['Instances']:
            instances[i['InstanceId']] = {
                'State': i['State']['Name'],
                'LaunchTime': i['LaunchTime'],
                'PrivateIp': i.get('PrivateIpAddress')
            }
    return instances

def get_eks_nodes():
    # Uses kubectl which should be configured in the environment
    try:
        out = subprocess.check_output(['kubectl', 'get', 'nodes', '-o', 'json'], stderr=subprocess.STDOUT)
        data = json.loads(out)
        nodes = {}
        for item in data['items']:
            name = item['metadata']['name']
            # Find status
            conditions = item['status']['conditions']
            ready = next((c['status'] for c in conditions if c['type'] == 'Ready'), 'False')
            
            # Get external ID (EC2 Instance ID)
            # Usually in spec.providerID: aws:///ap-south-1/i-xxxx
            provider_id = item['spec'].get('providerID', '')
            instance_id = provider_id.split('/')[-1] if provider_id else 'unknown'
            
            nodes[instance_id] = {
                'Name': name,
                'Ready': ready,
                'Version': item['status']['nodeInfo']['kubeletVersion']
            }
        return nodes
    except Exception as e:
        print(f"Error getting kubectl nodes: {e}")
        return {}

def monitor_update(cluster_name, nodegroup_name, region):
    print(f"Starting monitoring for {nodegroup_name} in {cluster_name}...")
    
    try:
        asg_name = get_asg_name(cluster_name, nodegroup_name, region)
        print(f"Found ASG: {asg_name}")
    except Exception as e:
        print(f"Error finding ASG: {e}")
        return

    while True:
        print("\n" + "="*80)
        print(f"Time: {time.strftime('%H:%M:%S')}")
        
        try:
            # 1. ASG Status
            asg = get_asg_details(asg_name, region)
            desired = asg['DesiredCapacity']
            instances = asg['Instances']
            instance_ids = [i['InstanceId'] for i in instances]
            
            print(f"ASG Status: Desired={desired}, Current={len(instances)}")
            
            # 2. EC2 Instance Status
            ec2_details = get_instance_details(instance_ids, region)
            
            # 3. EKS Node Status
            eks_nodes = get_eks_nodes()
            
            # 4. Correlate and Display
            print(f"{'Instance ID':<20} {'EC2 State':<15} {'Lifecycle':<15} {'EKS Ready':<10} {'Kube Version':<15}")
            print("-" * 80)
            
            ready_count = 0
            
            for i in instances:
                iid = i['InstanceId']
                lifecycle = i['LifecycleState']
                ec2_state = ec2_details.get(iid, {}).get('State', 'Unknown')
                
                node_info = eks_nodes.get(iid, {})
                eks_ready = node_info.get('Ready', 'N/A')
                kube_ver = node_info.get('Version', '')
                
                if eks_ready == 'True':
                    ready_count += 1
                    
                print(f"{iid:<20} {ec2_state:<15} {lifecycle:<15} {eks_ready:<10} {kube_ver:<15}")

            # Check for completion
            eks = boto3.client('eks', region_name=region)
            ng_resp = eks.describe_nodegroup(clusterName=cluster_name, nodegroupName=nodegroup_name)
            ng_status = ng_resp['nodegroup']['status']
            print(f"\nNode Group Status: {ng_status}")
            
            # Completion Logic:
            # - Node Group is ACTIVE
            # - ASG instances match desired
            # - All instances InService
            # - All instances Ready in EKS
            # - (Optional) Old nodes are gone? 
            #   If Desired is 1, and we have 1 instance that is Ready and InService, we are good.
            #   During update, Desired might be 2 (1 old, 1 new).
            #   We should wait until Desired stabilizes and matches our healthy count.
            
            in_service_count = sum(1 for i in instances if i['LifecycleState'] == 'InService')
            
            if ng_status == 'ACTIVE' and len(instances) == desired and ready_count == desired and in_service_count == desired:
                print("\n✅ Update Complete! All nodes are Active, Ready, and InService.")
                break
                
        except Exception as e:
            print(f"Error in monitoring loop: {e}")
            
        time.sleep(30)

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python3 monitor.py <cluster_name> <nodegroup_name> <region>")
        sys.exit(1)
        
    monitor_update(sys.argv[1], sys.argv[2], sys.argv[3])

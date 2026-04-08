import argparse
import base64
import json
import os
import re
import uuid

import boto3
from botocore.exceptions import ClientError

STATE_FILE = "state.json"
AMI_PARAM = "/aws/service/ami-amazon-linux-latest/amzn2-ami-hvm-x86_64-gp2"


def clean_name(value):
    value = re.sub(r"[^A-Za-z0-9-]", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "app"


def make_name(prefix, suffix, max_len):
    base = clean_name(f"{prefix}-{suffix}")
    if len(base) <= max_len:
        return base
    keep = max_len - len(suffix) - 1
    if keep < 1:
        return base[:max_len]
    return f"{clean_name(prefix)[:keep]}-{suffix}"


def get_vpc_id(ec2, provided_vpc_id):
    if provided_vpc_id:
        return provided_vpc_id
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if vpcs:
        return vpcs[0]["VpcId"]
    igws = ec2.describe_internet_gateways()["InternetGateways"]
    vpc_ids = []
    for igw in igws:
        for attachment in igw.get("Attachments", []):
            if attachment.get("State") == "available" and attachment.get("VpcId"):
                vpc_ids.append(attachment["VpcId"])
    for vpc_id in dict.fromkeys(vpc_ids):
        try:
            get_two_subnets(ec2, vpc_id, None)
        except RuntimeError:
            continue
        return vpc_id
    raise RuntimeError(
        "No default VPC found. Provide --vpc-id and --subnet-ids for a VPC with an internet gateway."
    )


def get_two_subnets(ec2, vpc_id, provided_subnet_ids):
    if provided_subnet_ids:
        ids = [item.strip() for item in provided_subnet_ids.split(",") if item.strip()]
        if len(ids) < 2:
            raise RuntimeError("Provide at least two subnet IDs.")
        return ids[:2]
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    if len(subnets) < 2:
        raise RuntimeError("VPC needs at least two subnets.")
    subnets.sort(key=lambda s: (s["AvailabilityZone"], s["SubnetId"]))
    selected = []
    used_azs = set()
    for subnet in subnets:
        az = subnet.get("AvailabilityZone")
        if az in used_azs:
            continue
        selected.append(subnet["SubnetId"])
        used_azs.add(az)
        if len(selected) == 2:
            break
    if len(selected) < 2:
        raise RuntimeError("Need subnets in at least two Availability Zones.")
    return selected


def get_or_create_sg(ec2, vpc_id, name, description):
    existing = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [name]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )["SecurityGroups"]
    if existing:
        return existing[0]["GroupId"]
    response = ec2.create_security_group(
        GroupName=name,
        Description=description,
        VpcId=vpc_id,
    )
    return response["GroupId"]


def add_ingress_rule(ec2, group_id, ip_permissions):
    try:
        ec2.authorize_security_group_ingress(
            GroupId=group_id,
            IpPermissions=ip_permissions,
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "InvalidPermission.Duplicate":
            raise


def create_vpc_resources(ec2):
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]
    vpc_id = vpc["VpcId"]

    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

    igw_id = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

    route_table_id = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
    ec2.create_route(
        RouteTableId=route_table_id,
        DestinationCidrBlock="0.0.0.0/0",
        GatewayId=igw_id,
    )

    azs = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])[
        "AvailabilityZones"
    ]
    if len(azs) < 2:
        raise RuntimeError("Need at least two Availability Zones to create subnets.")

    cidrs = ["10.0.1.0/24", "10.0.2.0/24"]
    subnet_ids = []
    for index in range(2):
        subnet = ec2.create_subnet(
            VpcId=vpc_id,
            AvailabilityZone=azs[index]["ZoneName"],
            CidrBlock=cidrs[index],
        )["Subnet"]
        subnet_id = subnet["SubnetId"]
        ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True})
        ec2.associate_route_table(SubnetId=subnet_id, RouteTableId=route_table_id)
        subnet_ids.append(subnet_id)

    return vpc_id, subnet_ids, igw_id, route_table_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", required=True, help="AWS region, e.g. us-east-1")
    parser.add_argument("--name-prefix", default="aws-webapp-autoscaling")
    parser.add_argument("--instance-type", default="t3.micro")
    parser.add_argument("--vpc-id", help="VPC ID to use (defaults to the default VPC)")
    parser.add_argument("--subnet-ids", help="Comma-separated subnet IDs (needs at least two)")
    parser.add_argument("--create-vpc", action="store_true", help="Create a new VPC and two public subnets")
    parser.add_argument("--min-size", type=int, default=1)
    parser.add_argument("--max-size", type=int, default=2)
    parser.add_argument("--desired", type=int, default=1)
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    ec2 = session.client("ec2")
    elbv2 = session.client("elbv2")
    autoscaling = session.client("autoscaling")
    ssm = session.client("ssm")

    created_vpc = False
    igw_id = None
    route_table_id = None

    if args.create_vpc:
        vpc_id, subnet_ids, igw_id, route_table_id = create_vpc_resources(ec2)
        created_vpc = True
    else:
        vpc_id = get_vpc_id(ec2, args.vpc_id)
        subnet_ids = get_two_subnets(ec2, vpc_id, args.subnet_ids)

    alb_sg_name = clean_name(f"{args.name_prefix}-alb-sg")
    app_sg_name = clean_name(f"{args.name_prefix}-app-sg")

    alb_sg_id = get_or_create_sg(ec2, vpc_id, alb_sg_name, "ALB HTTP access")
    app_sg_id = get_or_create_sg(ec2, vpc_id, app_sg_name, "App server access")

    add_ingress_rule(
        ec2,
        alb_sg_id,
        [
            {
                "IpProtocol": "tcp",
                "FromPort": 80,
                "ToPort": 80,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
        ],
    )
    add_ingress_rule(
        ec2,
        app_sg_id,
        [
            {
                "IpProtocol": "tcp",
                "FromPort": 80,
                "ToPort": 80,
                "UserIdGroupPairs": [{"GroupId": alb_sg_id}],
            }
        ],
    )

    ami_id = ssm.get_parameter(Name=AMI_PARAM)["Parameter"]["Value"]

    suffix = uuid.uuid4().hex[:6]
    base_name = clean_name(f"{args.name_prefix}-{suffix}")
    lb_name = make_name(args.name_prefix, suffix, 32)
    tg_name = make_name(f"{args.name_prefix}-tg", suffix, 32)
    lt_name = f"{base_name}-lt"
    asg_name = f"{base_name}-asg"

    tg_response = elbv2.create_target_group(
        Name=tg_name,
        Protocol="HTTP",
        Port=80,
        VpcId=vpc_id,
        HealthCheckPath="/",
        TargetType="instance",
    )
    target_group_arn = tg_response["TargetGroups"][0]["TargetGroupArn"]

    lb_response = elbv2.create_load_balancer(
        Name=lb_name,
        Subnets=subnet_ids,
        SecurityGroups=[alb_sg_id],
        Scheme="internet-facing",
        Type="application",
        IpAddressType="ipv4",
    )
    lb = lb_response["LoadBalancers"][0]
    lb_arn = lb["LoadBalancerArn"]
    lb_dns = lb["DNSName"]

    listener_response = elbv2.create_listener(
        LoadBalancerArn=lb_arn,
        Protocol="HTTP",
        Port=80,
        DefaultActions=[{"Type": "forward", "TargetGroupArn": target_group_arn}],
    )
    listener_arn = listener_response["Listeners"][0]["ListenerArn"]

    user_data = """#!/bin/bash
yum update -y
yum install -y httpd
systemctl enable httpd
systemctl start httpd
cat > /var/www/html/index.html <<'EOF'
<html>
  <head>
    <title>AWS Auto Scaling Demo</title>
  </head>
  <body>
    <h1>It works</h1>
    <p>Served from: $(hostname)</p>
  </body>
</html>
EOF
"""
    user_data_b64 = base64.b64encode(user_data.encode("utf-8")).decode("utf-8")

    lt_response = ec2.create_launch_template(
        LaunchTemplateName=lt_name,
        LaunchTemplateData={
            "ImageId": ami_id,
            "InstanceType": args.instance_type,
            "SecurityGroupIds": [app_sg_id],
            "UserData": user_data_b64,
        },
    )
    lt_id = lt_response["LaunchTemplate"]["LaunchTemplateId"]

    autoscaling.create_auto_scaling_group(
        AutoScalingGroupName=asg_name,
        MinSize=args.min_size,
        MaxSize=args.max_size,
        DesiredCapacity=args.desired,
        VPCZoneIdentifier=",".join(subnet_ids),
        LaunchTemplate={"LaunchTemplateId": lt_id, "Version": "$Latest"},
        TargetGroupARNs=[target_group_arn],
        HealthCheckType="ELB",
        HealthCheckGracePeriod=120,
    )

    state = {
        "region": args.region,
        "vpc_id": vpc_id,
        "subnet_ids": subnet_ids,
        "created_vpc": created_vpc,
        "internet_gateway_id": igw_id,
        "route_table_id": route_table_id,
        "created_subnet_ids": subnet_ids if created_vpc else [],
        "alb_sg_id": alb_sg_id,
        "app_sg_id": app_sg_id,
        "load_balancer_arn": lb_arn,
        "load_balancer_dns": lb_dns,
        "target_group_arn": target_group_arn,
        "listener_arn": listener_arn,
        "launch_template_id": lt_id,
        "launch_template_name": lt_name,
        "auto_scaling_group_name": asg_name,
    }

    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)

    print(f"Load balancer DNS: {lb_dns}")
    print(f"State saved to {os.path.abspath(STATE_FILE)}")


if __name__ == "__main__":
    main()

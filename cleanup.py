import argparse
import json
import os
import time

import boto3
from botocore.exceptions import ClientError

STATE_FILE = "state.json"


def load_state():
    if not os.path.exists(STATE_FILE):
        raise FileNotFoundError("state.json not found. Run deploy.py first.")
    with open(STATE_FILE, "r", encoding="utf-8") as handle:
        return json.load(handle)


def safe_call(action, label, ignore_codes=None):
    ignore_codes = ignore_codes or set()
    try:
        action()
        print(f"Deleted {label}")
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ignore_codes:
            print(f"{label} not found or already removed")
        else:
            print(f"Could not delete {label}: {code}")


def delete_route_table_associations(ec2, route_table_id):
    try:
        response = ec2.describe_route_tables(RouteTableIds=[route_table_id])
    except ClientError:
        return
    tables = response.get("RouteTables", [])
    if not tables:
        return
    associations = tables[0].get("Associations", [])
    for association in associations:
        assoc_id = association.get("RouteTableAssociationId")
        if assoc_id and not association.get("Main"):
            safe_call(
                lambda assoc_id=assoc_id: ec2.disassociate_route_table(
                    AssociationId=assoc_id
                ),
                f"route table association {assoc_id}",
                ignore_codes={"InvalidAssociationID.NotFound"},
            )


def get_instance_ids(ec2, vpc_id):
    response = ec2.describe_instances(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    instances = []
    for reservation in response.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            state = instance.get("State", {}).get("Name")
            if state != "terminated":
                instances.append(instance["InstanceId"])
    return instances


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", help="AWS region, e.g. us-east-1")
    args = parser.parse_args()

    state = load_state()
    region = args.region or state.get("region")
    if not region:
        raise RuntimeError("Region not provided and not found in state.json.")

    session = boto3.Session(region_name=region)
    ec2 = session.client("ec2")
    elbv2 = session.client("elbv2")
    autoscaling = session.client("autoscaling")

    asg_name = state.get("auto_scaling_group_name")
    if asg_name:
        safe_call(
            lambda: autoscaling.delete_auto_scaling_group(
                AutoScalingGroupName=asg_name, ForceDelete=True
            ),
            f"Auto Scaling group {asg_name}",
            ignore_codes={"ValidationError", "ResourceInUse"},
        )

    lt_id = state.get("launch_template_id")
    if lt_id:
        safe_call(
            lambda: ec2.delete_launch_template(LaunchTemplateId=lt_id),
            f"launch template {lt_id}",
            ignore_codes={"InvalidLaunchTemplateId.NotFound"},
        )

    listener_arn = state.get("listener_arn")
    if listener_arn:
        safe_call(
            lambda: elbv2.delete_listener(ListenerArn=listener_arn),
            f"listener {listener_arn}",
            ignore_codes={"ListenerNotFound"},
        )

    lb_arn = state.get("load_balancer_arn")
    if lb_arn:
        safe_call(
            lambda: elbv2.delete_load_balancer(LoadBalancerArn=lb_arn),
            f"load balancer {lb_arn}",
            ignore_codes={"LoadBalancerNotFound"},
        )
        try:
            waiter = elbv2.get_waiter("load_balancers_deleted")
            waiter.wait(LoadBalancerArns=[lb_arn])
        except ClientError:
            pass

    target_group_arn = state.get("target_group_arn")
    if target_group_arn:
        safe_call(
            lambda: elbv2.delete_target_group(TargetGroupArn=target_group_arn),
            f"target group {target_group_arn}",
            ignore_codes={"TargetGroupNotFound"},
        )

    alb_sg_id = state.get("alb_sg_id")
    if alb_sg_id:
        safe_call(
            lambda: ec2.delete_security_group(GroupId=alb_sg_id),
            f"security group {alb_sg_id}",
            ignore_codes={"InvalidGroup.NotFound", "DependencyViolation"},
        )

    app_sg_id = state.get("app_sg_id")
    if app_sg_id:
        safe_call(
            lambda: ec2.delete_security_group(GroupId=app_sg_id),
            f"security group {app_sg_id}",
            ignore_codes={"InvalidGroup.NotFound", "DependencyViolation"},
        )

    if state.get("created_vpc"):
        vpc_id = state.get("vpc_id")
        if vpc_id:
            instance_ids = get_instance_ids(ec2, vpc_id)
            if instance_ids:
                try:
                    ec2.terminate_instances(InstanceIds=instance_ids)
                    waiter = ec2.get_waiter("instance_terminated")
                    waiter.wait(InstanceIds=instance_ids)
                    print(f"Terminated instances: {', '.join(instance_ids)}")
                except ClientError as exc:
                    print(f"Could not terminate instances: {exc.response['Error']['Code']}")

        delete_route_table_associations(ec2, state.get("route_table_id"))

        for subnet_id in state.get("created_subnet_ids", []):
            safe_call(
                lambda subnet_id=subnet_id: ec2.delete_subnet(SubnetId=subnet_id),
                f"subnet {subnet_id}",
                ignore_codes={"InvalidSubnetID.NotFound", "DependencyViolation"},
            )

        igw_id = state.get("internet_gateway_id")
        if igw_id and vpc_id:
            safe_call(
                lambda: ec2.detach_internet_gateway(
                    InternetGatewayId=igw_id, VpcId=vpc_id
                ),
                f"internet gateway detach {igw_id}",
                ignore_codes={"Gateway.NotAttached"},
            )
            safe_call(
                lambda: ec2.delete_internet_gateway(InternetGatewayId=igw_id),
                f"internet gateway {igw_id}",
                ignore_codes={"InvalidInternetGatewayID.NotFound", "DependencyViolation"},
            )

        route_table_id = state.get("route_table_id")
        if route_table_id:
            safe_call(
                lambda: ec2.delete_route_table(RouteTableId=route_table_id),
                f"route table {route_table_id}",
                ignore_codes={"InvalidRouteTableID.NotFound", "DependencyViolation"},
            )

        if vpc_id:
            safe_call(
                lambda: ec2.delete_vpc(VpcId=vpc_id),
                f"vpc {vpc_id}",
                ignore_codes={"InvalidVpcID.NotFound", "DependencyViolation"},
            )

    time.sleep(1)
    try:
        os.remove(STATE_FILE)
        print("Removed state.json")
    except OSError:
        pass


if __name__ == "__main__":
    main()

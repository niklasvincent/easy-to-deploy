#!/usr/bin/env python
import argparse
import ConfigParser
import sys
import os
import logging
import time


class ElasticLoadBalancer(object):

    def __init__(self, client, name):
        self.client = client
        self.name = name
        self._update_details()

    def _update_details(self):
        """Update cached details about this ELB instance"""
        self.details = self.client.describe_load_balancers(
            LoadBalancerNames=[self.name]
        )['LoadBalancerDescriptions'][0]

    def get_instance_health(self):
        """Get instance health for all instances"""
        response = self.client.describe_instance_health(
            LoadBalancerName=self.name
        )
        return response['InstanceStates']

    def get_total_instance_count(self):
        """Get total instance count"""
        return len([i for i in self.get_instance_health()])

    def get_healthy_instance_count(self):
        """Get healthy instance count"""
        healthy_instance_count = 0
        for i in self.get_instance_health():
            if i['State'] == 'InService':
                healthy_instance_count += 1
        return healthy_instance_count


class AutoScalingGroup(object):

    def __init__(self, client, name):
        self.processes = [
            'AlarmNotification'
        ]
        self.name = name
        self.client = client
        self._update_details()

    def _update_details(self):
        """Update cached details about this ASG instance"""
        self.details = self.client.describe_auto_scaling_groups(
            AutoScalingGroupNames=[self.name],
            MaxRecords=1
        )['AutoScalingGroups'][0]

    def resume_processes(self):
        """Enable normal operational processes"""
        response = self.client.resume_processes(
            AutoScalingGroupName=self.name,
            ScalingProcesses=self.processes
        )

    def suspend_processes(self):
        """Suspend normal operational processes"""
        self.client.suspend_processes(
            AutoScalingGroupName=self.name,
            ScalingProcesses=self.processes
        )

    def has_enough_capacity(self):
        """Check whether this ASG has enough capacity to double in size"""
        return self.get_new_desired_capacity() <= self.get_max_capacity()

    def get_desired_capacity(self):
        """Get current desired capacity"""
        self._update_details()
        desired_capacity = int(self.details["DesiredCapacity"])
        return desired_capacity

    def get_new_desired_capacity(self):
        """Get new desired capacity"""
        self._update_details()
        desired_capacity = int(self.details["DesiredCapacity"])
        new_desired_capacity = 2 * desired_capacity
        return new_desired_capacity

    def get_max_capacity(self):
        """Get current max capacity"""
        self._update_details()
        max_capacity = int(self.details["MaxSize"])
        return max_capacity

    def get_load_balancer_name(self):
        """Get name of the first ELB associated with this ASG"""
        self._update_details()
        load_balancer_name = self.details['LoadBalancerNames'][0]
        return load_balancer_name

    def get_instance_ids(self):
        """Get the IDs of all instances in this ASG"""
        self._update_details()
        instance_ids = [i["InstanceId"] for i in self.details["Instances"]]
        return instance_ids

    def get_healthy_instance_count(self):
        healthy_instance_count = 0
        response = self.client.describe_auto_scaling_instances(
            MaxRecords=50,
        )['AutoScalingInstances']
        for i in response:
            if i['LifecycleState'] == 'InService':
                healthy_instance_count += 1
        return healthy_instance_count

    def set_desired_capacity(self, new_desired_capacity):
        """Set new desired capacity of this ASG"""
        response = self.client.set_desired_capacity(
            AutoScalingGroupName=self.name,
            DesiredCapacity=new_desired_capacity,
            HonorCooldown=False
        )

    def double_in_size(self):
        """Double the desired capacity of this ASG"""
        new_desired_capacity = self.get_new_desired_capacity()
        self.set_desired_capacity(new_desired_capacity)


class EC2(object):

    def __init__(self, client):
        self.client = client
        self._termination_tag = {
            'Key': 'Terminate-After-Deploy',
            'Value': 'true'
        }

    def tag_instances(self, instance_ids, tags):
        """Add a tag to a list of instances"""
        response = self.client.create_tags(
            DryRun=False,
            Resources=instance_ids,
            Tags=tags
        )

    def mark_instances_for_termination(self, instance_ids):
        """Mark a list of instances for termination"""
        self.tag_instances(
            instance_ids=instance_ids,
            tags=[
                self._termination_tag
            ]
        )

    def get_instances_marked_for_termination(self):
        """Get a list of instances marked for termination"""
        try:
            response = self.client.describe_instances(
                DryRun=False,
                Filters=[
                    {
                        'Name': 'tag:%s' % self._termination_tag['Key'],
                        'Values': [
                            self._termination_tag['Value']
                        ]
                    }
                ]
            )['Reservations'][0]['Instances']
            return [i['InstanceId'] for i in response]
        except:
            return []

    def terminate_instances(self, instance_ids):
        """Terminate a list of instances"""
        if instance_ids and len(instance_ids) > 0:
            response = self.client.terminate_instances(
                DryRun=False,
                InstanceIds=instance_ids
            )
            return response


def setup_clients(profile_name, region_name):
    try:
        import boto3
        import botocore.session
    except:
        logging.error("Could not import \'boto3\' library. Please install.")
        sys.exit(3)
    session = boto3.session.Session(profile_name=profile_name)
    asg_client = session.client('autoscaling', region_name=region_name)
    ec2_client = session.client('ec2', region_name=region_name)
    elb_client = session.client('elb', region_name=region_name)
    return asg_client, ec2_client, elb_client


def deploy(auto_scaling_group_name, asg_client, ec2_client, elb_client):
    asg = AutoScalingGroup(client=asg_client, name=auto_scaling_group_name)
    ec2 = EC2(client=ec2_client)

    elb_name = asg.get_load_balancer_name()
    elb = ElasticLoadBalancer(client=elb_client, name=elb_name)

    if not asg.has_enough_capacity():
        logging.error(
            "Not enough capacity in auto scaling group for deployment"
        )
        sys.exit(4)

    original_desired_capacity = asg.get_desired_capacity()
    logging.info("Original desired capacity is %d", original_desired_capacity)

    logging.info("Marking existing instances for post-deployment termination")
    existing_instances = asg.get_instance_ids()
    ec2.mark_instances_for_termination(existing_instances)

    logging.info("Suspending auto scaling group processes")
    asg.suspend_processes()

    logging.info("Doubling auto scaling group size")
    asg.double_in_size()

    current_desired_capacity = asg.get_desired_capacity()

    time_spent_waiting = 0
    # Wait for instances to be healthy and recognised by ASG
    # Time out if not happening within 15 minutes
    current_asg_healhty_instances = asg.get_healthy_instance_count()
    while current_asg_healhty_instances < current_desired_capacity:
        logging.info(
            "Waiting for ASG to recognise %d healthy instances. Currently %d",
            current_desired_capacity,
            current_asg_healhty_instances
        )
        time.sleep(30)
        time_spent_waiting += 30
        if time_spent_waiting >= 900:
            logging.error("New hosts not healthy within 15 minutes")
            sys.exit(5)
        current_asg_healhty_instances = asg.get_healthy_instance_count()

    time_spent_waiting = 0
    # Wait for instances to be healthy and recognised by ElasticLoadBalancer
    # Time out if not happening within 15 minutes
    current_elb_healthy_instances = elb.get_healthy_instance_count()
    while current_elb_healthy_instances < current_desired_capacity:
        logging.info(
            "Waiting for ELB to recognise %d healthy instances. Currently %d",
            current_desired_capacity,
            current_elb_healthy_instances
        )
        time.sleep(30)
        time_spent_waiting += 30
        if time_spent_waiting >= 900:
            logging.error("New hosts not healthy within 15 minutes")
            sys.exit(5)
        current_elb_healthy_instances = elb.get_healthy_instance_count()

    logging.info(
        "ELB has recognised %d healthy instances",
        current_elb_healthy_instances
    )

    logging.info(
        "ASG has recognised %d healthy instances",
        current_asg_healhty_instances
    )

    # Terminate instances marked for termination
    logging.info("Terminating previous instances")
    instances_to_terminate = ec2.get_instances_marked_for_termination()
    ec2.terminate_instances(instances_to_terminate)

    # Set desired capacity to original
    logging.info(
        "Setting current capacity to original %d",
        original_desired_capacity
    )
    asg.set_desired_capacity(original_desired_capacity)

    logging.info("Resuming auto scaling group processes")
    asg.resume_processes()


def setup_logging(debug=False):
    root = logging.getLogger()
    ch = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    root.addHandler(ch)
    if debug:
        ch.setLevel(logging.DEBUG)
        root.setLevel(logging.DEBUG)
    else:
        ch.setLevel(logging.INFO)
        root.setLevel(logging.INFO)


def parse_arguments():
    """Parse command line arguments"""
    args_parser = argparse.ArgumentParser()
    args_parser.add_argument(
        "--debug",
        help="Increase output verbosity",
        action="store_true")
    args_parser.add_argument(
        "--profile",
        required=False,
        default="ec2",
        help="AWS profile to use")
    args_parser.add_argument(
        "--region",
        required=False,
        default="eu-west-1",
        help="AWS region to use")
    args_parser.add_argument(
        "--application",
        required=True,
        help="Application to deploy")
    args_parser.add_argument(
        "--config",
        required=True,
        help="Configuration file")
    args = args_parser.parse_args()
    return args


def main():
    arguments = parse_arguments()
    setup_logging(arguments.debug)
    config = ConfigParser.ConfigParser()
    try:
        config.read(arguments.config)
    except Exception as e:
        logging.error(
            "Could not parse configuration file %s: %s",
            configuration_filename,
            e
        )
        sys.exit(1)

    try:
        auto_scaling_group = config.get(
            arguments.application,
            "auto_scaling_group"
        )
    except Exception as e:
        logging.error(
            "Could not find configuration for application \'%s\'",
            arguments.application
        )
        sys.exit(2)

    asg_client, ec2_client, elb_client = setup_clients(
        profile_name=arguments.profile,
        region_name=arguments.region
    )

    deploy(auto_scaling_group, asg_client, ec2_client, elb_client)

if __name__ == '__main__':
    main()

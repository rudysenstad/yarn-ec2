#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
import os
import random
import string
import subprocess
import sys
from sys import stderr
import time
from boto import ec2
from optparse import OptionParser
import ec2_util

class UsageError(Exception):
    pass

# Configure and parse our command-line arguments
def parse_args():
    parser = OptionParser(
        usage="mode-ec2 [options] <action> <cluster_name>"
        + "\n\n<action> can be: launch, addslave, addspot, login, get-master, forward-port",
        add_help_option=False)
    parser.add_option(
        "-h", "--help", action="help",
        help="Show this help message and exit")
    parser.add_option(
        "-s", "--slaves", type="int", default=1,
        help="Number of slaves to launch (default: 1)")
    parser.add_option(
        "-w", "--wait", type="int", default=120,
        help="Seconds to wait for nodes to start (default: 120)")
    parser.add_option(
        "-k", "--key-pair",
        help="Key pair to use on instances")
    parser.add_option(
        "-i", "--identity-file",
        help="SSH private key file to use for logging into instances")
    parser.add_option(
        "-t", "--instance-type", default="c3.2xlarge",
        help="Type of instance to launch (default: m3.xlarge). " +
             "WARNING: must be 64-bit; small instances won't work")
    parser.add_option(
        "-r", "--region", default="us-east-1",
        help="EC2 region zone to launch instances in")
    parser.add_option(
        "-z", "--zone", default="us-east-1c",
        help="Availability zone to launch instances in, or 'all' to spread " +
             "slaves across multiple (an additional $0.01/Gb for bandwidth" +
             "between zones applies)")
    parser.add_option("-a", "--ami", help="Amazon Machine Image ID to use")
    parser.add_option(
        "--include-aws-key", default=False,
        help=("Whether include aws key information in bootstrap script," +
              " this can be very dangerous as boostrap script is not encrypted"))
    parser.add_option(
        "--spot-price", metavar="PRICE", type="float",
        help="If specified, launch slaves as spot instances with the given " +
             "maximum price (in dollars)")
    parser.add_option(
        "-u", "--user", default="ubuntu",
        help="The SSH user you want to connect as (default: root)")
    parser.add_option(
        "--delete-groups", action="store_true", default=False,
        help="When destroying a cluster, delete the security groups that were created")
    parser.add_option(
        "--vpc", default=None,
        help="VPC to add to EC2 instances in addition to the yarn specific groups")
    parser.add_option(
        "-S", "--subnet", default='subnet-a67322d0',
        help="Subnet-ID in the VPC specified")

    (opts, args) = parser.parse_args()
    if len(args) != 2:
        parser.print_help()
        sys.exit(1)
    action, cluster_name = args
    opts.action = action
    opts.cluster_name = cluster_name
    # Boto config check
    # http://boto.cloudhackers.com/en/latest/boto_config_tut.html
    home_dir = os.getenv('HOME')
    if home_dir is None or not os.path.isfile(home_dir + '/.boto'):
        if not os.path.isfile('/etc/boto.cfg'):
            if os.getenv('AWS_ACCESS_KEY_ID') is None:
                print >> stderr, ("ERROR: The environment variable AWS_ACCESS_KEY_ID " +
                                  "must be set")
                sys.exit(1)
            if os.getenv('AWS_SECRET_ACCESS_KEY') is None:
                print >> stderr, ("ERROR: The environment variable AWS_SECRET_ACCESS_KEY " +
                                  "must be set")
                sys.exit(1)
    return opts

def get_resource_map(fname = 'data/instance.matrix.txt'):
    vcpu = {}
    vram = {}
    price = {}
    for l in open(fname):
        if len(l.strip()) == 0:
            continue
        arr = l.split('\t')
        if len(arr) != 0:
            vcpu[arr[0]] = int(arr[1])
            vram[arr[0]] = int(float(arr[3]) * 1024)
            price[arr[0]] = float(arr[5].split()[0].strip('$'))
    return vcpu, vram, price

#
# get user data of specific instance
#
def get_user_data(fname, master_dns, instance_type, include_aws_key):
    vcpu, vram, price = get_resource_map()
    data = open(fname).readlines()
    ret = []
    if include_aws_key:
        print("include AWS key option is switched on...")

    for l in data:
        special = True
        if l.startswith('MASTER ='):
            ret.append('MASTER = \'%s\'\n' % master_dns)
        elif l.startswith('NODE_TYPE ='):
            ret.append('NODE_TYPE = \'%s\'\n' % instance_type)
        elif l.startswith('NODE_VMEM ='):
            ret.append('NODE_VMEM = %d\n' % vram[instance_type])
        elif l.startswith('NODE_VCPU ='):
            ret.append('NODE_VCPU = %d\n' % vcpu[instance_type])
        elif l.startswith('AWS_KEY =') and include_aws_key:
            ret.append('AWS_KEY = \'%s\'\n' % os.getenv('AWS_SECRET_ACCESS_KEY', 'undefined'))
        elif l.startswith('AWS_ID =') and include_aws_key:
            ret.append('AWS_ID = \'%s\'\n' % os.getenv('AWS_ACCESS_KEY_ID', 'undefined'))
        else:
            ret.append(l)
            special = False
    udata = ''.join(ret)
    return udata

# get ami of the machine
# use ubuntu machines
def get_ami(instance):
    itype = ec2_util.get_instance_type(instance)
    if itype == 'pvm':
        return 'ami-6989a659'
    else:
        return 'ami-5189a661'
        
def delete_security_group(security_group, dependent_group):
    if security_group and security_group.rules:
        for rule in security_group.rules:
            try:
                security_group.revoke(rule.ip_protocol, rule.from_port, rule.to_port, '0.0.0.0/0')
            except:
                print("Error revoking {}".format(rule))
        rule_info = [['icmp', -1, -1], ['tcp', 1, 65535], ['udp', 1, 65535]]
        groups = [dependent_group, security_group]
        for this_group in groups:
            for rule in rule_info:
                if this_group:
                    try:
                        security_group.revoke(rule[0], rule[1], rule[2], src_group=this_group)
                    except:
                        continue
    


def delete_groups(conn, cluster_name):
    master_group = ec2_util.get_or_make_group(conn, cluster_name + "-master", create=False)
    slave_group = ec2_util.get_or_make_group(conn, cluster_name + "-slave", create=False)
    if master_group:
        delete_security_group(master_group, slave_group)
    if slave_group:
        delete_security_group(slave_group, master_group)
    if master_group:
        master_group.delete()
    if slave_group:
        slave_group.delete()

# Launch master of a cluster of the given name, by setting up its security groups,
# and then starting new instances in them.
# Returns a tuple of EC2 reservation objects for the master and slaves
# Fails if there already instances running in the cluster's groups.
def launch_master(conn, opts):
    cluster_name = opts.cluster_name
    if opts.identity_file is None:
        print >> stderr, "ERROR: Must provide an identity file (-i) for ssh connections."
        sys.exit(1)
    if opts.key_pair is None:
        print >> stderr, "ERROR: Must provide a key pair name (-k) to use on instances."
        sys.exit(1)

    print "Setting up security groups..."
    master_group = ec2_util.get_or_make_group(conn, cluster_name + "-master", vpc_id=opts.vpc)
    slave_group = ec2_util.get_or_make_group(conn, cluster_name + "-slave", vpc_id=opts.vpc)
    if master_group.rules == []:  # Group was just now created
#        master_group.authorize(src_group=master_group)
#        master_group.authorize(src_group=slave_group)
        master_group.authorize('udp', 1, 65535, src_group=slave_group)
        master_group.authorize('udp', 1, 65535, src_group=master_group)
        master_group.authorize('icmp', -1, -1, src_group=slave_group)
        master_group.authorize('icmp', -1, -1, src_group=master_group)
        master_group.authorize('tcp', 1, 65535, src_group=slave_group)
        master_group.authorize('tcp', 1, 65535, src_group=master_group)
        master_group.authorize('tcp', 22, 22, '0.0.0.0/0')
        master_group.authorize('tcp', 8000, 8100, '0.0.0.0/0')
        master_group.authorize('tcp', 9000, 9999, '0.0.0.0/0')
        master_group.authorize('tcp', 18080, 18080, '0.0.0.0/0')
        master_group.authorize('tcp', 19999, 19999, '0.0.0.0/0')
        master_group.authorize('tcp', 50000, 50100, '0.0.0.0/0')
        master_group.authorize('tcp', 60070, 60070, '0.0.0.0/0')
        master_group.authorize('tcp', 4040, 4045, '0.0.0.0/0')
        master_group.authorize('tcp', 5080, 5080, '0.0.0.0/0')
        master_group.authorize('udp', 0, 65535, '0.0.0.0/0')
    if slave_group.rules == []:  # Group was just now created
#        slave_group.authorize(src_group=master_group)
#        slave_group.authorize(src_group=slave_group)
        slave_group.authorize('icmp', -1, -1, src_group=slave_group)
        slave_group.authorize('icmp', -1, -1, src_group=master_group)
        slave_group.authorize('tcp', 1, 65535, src_group=slave_group)
        slave_group.authorize('tcp', 1, 65535, src_group=master_group)
        slave_group.authorize('udp', 1, 65535, src_group=slave_group)
        slave_group.authorize('udp', 1, 65535, src_group=master_group)
        slave_group.authorize('tcp', 22, 22, '0.0.0.0/0')
        slave_group.authorize('tcp', 8000, 8100, '0.0.0.0/0')
        slave_group.authorize('tcp', 9000, 9999, '0.0.0.0/0')
        slave_group.authorize('tcp', 50000, 50100, '0.0.0.0/0')
        slave_group.authorize('tcp', 60060, 60060, '0.0.0.0/0')
        slave_group.authorize('tcp', 60075, 60075, '0.0.0.0/0')
        slave_group.authorize('udp', 0, 65535, '0.0.0.0/0')

    # Check if instances are already running in our groups
    existing_masters, existing_slaves = ec2_util.get_existing_cluster(conn, cluster_name,
                                                                      die_on_error=False)
    print "existing_master: {}".format(existing_masters)
    print "existing_slaves: {}".format(existing_slaves)

    if existing_slaves:
        print >> stderr, ("ERROR: There are already instances running in " +
                          "group %s or %s" % (group.name, slave_group.name))
        sys.exit(1)

    if opts.ami is None:
        opts.ami = get_ami(opts.instance_type)
    print "Launching instances..."

    try:
        image = conn.get_all_images(image_ids=[opts.ami])[0]
    except:
        print >> stderr, "Could not find AMI " + opts.ami
        sys.exit(1)
    print "AMI Image: {}".format(image)
    # Launch or resume masters
    if existing_masters:
        print "Starting master..."
        for inst in existing_masters:
            if inst.state not in ["shutting-down", "terminated"]:
                inst.start()
        master_nodes = existing_masters
    else:
        # Create block device mapping so that we can add an EBS volume if asked to
        block_map = ec2_util.get_block_device(opts.instance_type, 0)
        master_type = opts.instance_type
        cpe_all_security_group = ec2_util.get_or_make_group(conn, "_CPE_allow_all_from_corp", create=False)
        security_group_ids = [master_group.id]
        if cpe_all_security_group:
            security_group_ids.append(cpe_all_security_group.id)
        if opts.zone == 'all':
            opts.zone = random.choice(conn.get_all_zones()).name
        print "Zone: {}".format(opts.zone)
        master_res = image.run(key_name=opts.key_pair,
#                               security_groups=[master_group],
                               instance_type=master_type,
                               placement=opts.zone,
                               min_count=1,
                               max_count=1,
                               block_device_map=block_map,
                               security_group_ids=security_group_ids,
                               subnet_id=opts.subnet,
                               user_data=get_user_data('bootstrap.py', '',
                                                       master_type, opts.include_aws_key))
        master_nodes = master_res.instances
        print "Launched master in %s, regid = %s" % (opts.zone, master_res.id)

    print 'Waiting for master to getup...'
    ec2_util.wait_for_instances(conn, master_nodes)

    # Give the instances descriptive names
    for master in master_nodes:
        master.add_tag(
            key='Name',
            value='{cn}-master-{iid}'.format(cn=cluster_name, iid=master.id))
    master = master_nodes[0].public_dns_name
    print 'finishing getting master %s' % master
    # Return all the instances
    return master_nodes

# Launch slaves of a cluster of the given name, by setting up its security groups,
# and then starting new instances in them.
# Returns a tuple of EC2 reservation objects for the master and slaves
# Fails if there already instances running in the cluster's groups.
def launch_slaves(conn, opts):
    cluster_name = opts.cluster_name
    if opts.identity_file is None:
        print >> sys.stderr, "ERROR: Must provide an identity file (-i) for ssh connections."
        sys.exit(1)
    if opts.key_pair is None:
        print >> sys.stderr, "ERROR: Must provide a key pair name (-k) to use on instances."
        sys.exit(1)
    master_group = ec2_util.get_or_make_group(conn, cluster_name + "-master", False)
    slave_group = ec2_util.get_or_make_group(conn, cluster_name + "-slave", False)
    # Check if instances are already running in our groups
    existing_masters, existing_slaves = ec2_util.get_existing_cluster(conn, cluster_name,
                                                                      die_on_error=False)
    if len(existing_masters) == 0:
        print >> stderr, ("ERROR: Cannot find master machine on group" +
                          "group %s" % (master_group.name))
        sys.exit(1)

    if opts.ami is None:
        opts.ami = get_ami(opts.instance_type)
    print "Launching instances..."

    try:
        image = conn.get_all_images(image_ids=[opts.ami])[0]
    except:
        print >> stderr, "Could not find AMI " + opts.ami
        sys.exit(1)
    cpe_all_security_group = ec2_util.get_or_make_group(conn, "_CPE_allow_all_from_corp", create=False)
    security_group_ids = [master_group.id]
    if cpe_all_security_group:
        security_group_ids.append(cpe_all_security_group.id)
    master = existing_masters[0]
    block_map = ec2_util.get_block_device(opts.instance_type, 0)
    zone = master.placement
    slave_res = image.run(key_name=opts.key_pair,
                          #security_groups=[slave_group],
                          instance_type=opts.instance_type,
                          placement=zone,
                          min_count=opts.slaves,
                          max_count=opts.slaves,
                          block_device_map=block_map,
                          security_group_ids=security_group_ids,
                          subnet_id=opts.subnet,
                          user_data=get_user_data('bootstrap.py',
                                                  master.private_dns_name,
                                                  opts.instance_type,
                                                  opts.include_aws_key))
    slave_nodes = slave_res.instances
    print "Launched %d slaves in %s, regid = %s" % (len(slave_nodes),
                                                    zone, slave_res.id)
    print 'Waiting for slave to getup...'
    ec2_util.wait_for_instances(conn, slave_nodes)
    for slave in slave_nodes:
        slave.add_tag(
            key='Name',
            value='{cn}-slave-{iid}'.format(cn=cluster_name, iid=slave.id))
    print 'Done...'

# Launch slaves of a cluster of the given name, by setting up its security groups,
# and then starting new instances in them.
# Returns a tuple of EC2 reservation objects for the master and slaves
# Fails if there already instances running in the cluster's groups.
def launch_spot_slaves(conn, opts):
    vcpu, vram, price = get_resource_map()
    cluster_name = opts.cluster_name
    if opts.identity_file is None:
        print >> sys.stderr, "ERROR: Must provide an identity file (-i) for ssh connections."
        sys.exit(1)
    if opts.spot_price is None:
        opts.spot_price = price[opts.instance_type]
        print "Spot price is not specified, bid the full price=%g for %s" % (opts.spot_price, opts.instance_type)

    if opts.key_pair is None:
        print >> sys.stderr, "ERROR: Must provide a key pair name (-k) to use on instances."
        sys.exit(1)

    master_group = ec2_util.get_or_make_group(conn, cluster_name + "-master", False)
    slave_group = ec2_util.get_or_make_group(conn, cluster_name + "-slave", False)
    # Check if instances are already running in our groups
    existing_masters, existing_slaves = ec2_util.get_existing_cluster(conn, cluster_name,
                                                                      die_on_error=False)
    if len(existing_masters) == 0:
        print >> stderr, ("ERROR: Cannot find master machine on group" +
                          "group %s" % (master_group.name))
        sys.exit(1)

    if opts.ami is None:
        opts.ami = get_ami(opts.instance_type)
    print "Launching Spot instances type=%s, price=%g..." % (opts.instance_type, opts.spot_price)

    master = existing_masters[0]
    block_map = ec2_util.get_block_device(opts.instance_type, 0)
    zone = master.placement
    slave_reqs = conn.request_spot_instances(
        price=opts.spot_price,
        image_id=opts.ami,
        launch_group="launch-group-%s" % cluster_name,
        placement=zone,
        count=opts.slaves,
        key_name=opts.key_pair,
        security_groups=[slave_group],
        instance_type=opts.instance_type,
        block_device_map=block_map,
        user_data=get_user_data('bootstrap.py',
                                master.private_dns_name,
                                opts.instance_type,
                                opts.include_aws_key))
    print 'Done... request is submitted'

def stringify_command(parts):
    if isinstance(parts, str):
        return parts
    else:
        return ' '.join(map(pipes.quote, parts))

def ssh_args(opts):
    parts = ['-o', 'StrictHostKeyChecking=no']
    if opts.identity_file is not None:
        parts += ['-i', opts.identity_file]
    return parts

def ssh_command(opts):
    return ['ssh'] + ssh_args(opts)

# Run a command on a host through ssh, retrying up to five times
# and then throwing an exception if ssh continues to fail.
def ssh(host, opts, command):
    tries = 0
    while True:
        try:
            return subprocess.check_call(
                ssh_command(opts) + ['-t', '-t', '%s@%s' % (opts.user, host),
                                     stringify_command(command)])
        except subprocess.CalledProcessError as e:
            if (tries > 5):
                # If this was an ssh failure, provide the user with hints.
                if e.returncode == 255:
                    raise UsageError(
                        "Failed to SSH to remote host {0}.\n" +
                        "Please check that you have provided the correct --identity-file and " +
                        "--key-pair parameters and try again.".format(host))
                else:
                    raise e
            print >> sys.stderr, \
                "Error executing remote command, retrying after 30 seconds: {0}".format(e)
            time.sleep(30)
            tries = tries + 1

def _check_output(*popenargs, **kwargs):
    if 'stdout' in kwargs:
        raise ValueError('stdout argument not allowed, it will be overridden.')
    process = subprocess.Popen(stdout=subprocess.PIPE, *popenargs, **kwargs)
    output, unused_err = process.communicate()
    retcode = process.poll()
    if retcode:
        cmd = kwargs.get("args")
        if cmd is None:
            cmd = popenargs[0]
        raise subprocess.CalledProcessError(retcode, cmd, output=output)
    return output

def main():
    logging.basicConfig()
    opts = parse_args()
    try:
        conn = ec2.connect_to_region(opts.region)
    except Exception as e:
        print >> sys.stderr, (e)
        sys.exit(1)

    if opts.zone == '':
        opts.zone = random.choice(conn.get_all_zones()).name

    action = opts.action
    cluster_name = opts.cluster_name

    if action == 'launch':
        master_nodes = launch_master(conn, opts)
    elif action == 'addslave':
        master_nodes = launch_slaves(conn, opts)
    elif action == 'addspot':
        master_nodes = launch_spot_slaves(conn, opts)
    elif action == "get-master":
        (master_nodes, slave_nodes) = ec2_util.get_existing_cluster(conn, cluster_name)
        print master_nodes[0].private_dns_name
    elif action == "login":
        (master_nodes, slave_nodes) = ec2_util.get_existing_cluster(conn, cluster_name)
        print(master_nodes)
#        master = master_nodes[0].public_dns_name
        master = master_nodes[0].private_dns_name
        subprocess.check_call(
            ssh_command(opts)  + ['-t', "%s@%s" % (opts.user, master)])
    elif action == "forward-port":
        (master_nodes, slave_nodes) = ec2_util.get_existing_cluster(conn, cluster_name)
#        master = master_nodes[0].public_dns_name
        master = master_nodes[0].private_dns_name
        subprocess.check_call(
            ssh_command(opts)  + ['-D', '9595'] + ['-t', "%s@%s" % (opts.user, master)])
#    elif action == "stop":
 #       (master_nodes, slave_nodes) = ec2_util.get_existing_cluster(conn, cluster_name)
  #      ec2_util.stop_instances(conn, master_nodes)
   #     ec2_util.stop_instances(conn, slave_nodes)
    elif action == "terminate":
        (master_nodes, slave_nodes) = ec2_util.get_existing_cluster(conn, cluster_name)
        ec2_util.terminate_instances(conn, master_nodes)
        ec2_util.terminate_instances(conn, slave_nodes)
        delete_groups(conn, cluster_name)
    else:
        print >> sys.stderr, "Invalid action: %s" % action
        sys.exit(1)


if __name__ == "__main__":
    main()

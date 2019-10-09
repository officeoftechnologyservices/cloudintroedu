import time
from ast import literal_eval
from ansible.module_utils.six import iteritems
from ansible.module_utils.six import get_function_code

try:
    import boto.ec2
    from boto.ec2.blockdevicemapping import BlockDeviceType, BlockDeviceMapping
    from boto.exception import EC2ResponseError
    from boto.vpc import VPCConnection
    HAS_BOTO = True
except ImportError:
    HAS_BOTO = False


def find_running_instances_by_count_tag(module, ec2, count_tag, zone=None):

    # get reservations for instances that match tag(s) and are running
    reservations = get_reservations(module, ec2, tags=count_tag, state="running", zone=zone)

    instances = []
    for res in reservations:
        if hasattr(res, 'instances'):
            for inst in res.instances:
                instances.append(inst)

    return reservations, instances


def _set_none_to_blank(dictionary):
    result = dictionary
    for k in result:
        if isinstance(result[k], dict):
            result[k] = _set_none_to_blank(result[k])
        elif not result[k]:
            result[k] = ""
    return result


def get_reservations(module, ec2, tags=None, state=None, zone=None):

    # TODO: filters do not work with tags that have underscores
    filters = dict()

    if tags is not None:

        if isinstance(tags, str):
            try:
                tags = literal_eval(tags)
            except:
                pass

        # if string, we only care that a tag of that name exists
        if isinstance(tags, str):
            filters.update({"tag-key": tags})

        # if list, append each item to filters
        if isinstance(tags, list):
            for x in tags:
                if isinstance(x, dict):
                    x = _set_none_to_blank(x)
                    filters.update(dict(("tag:"+tn, tv) for (tn,tv) in iteritems(x)))
                else:
                    filters.update({"tag-key": x})

        # if dict, add the key and value to the filter
        if isinstance(tags, dict):
            tags = _set_none_to_blank(tags)
            filters.update(dict(("tag:"+tn, tv) for (tn,tv) in iteritems(tags)))

    if state:
        # http://stackoverflow.com/questions/437511/what-are-the-valid-instancestates-for-the-amazon-ec2-api
        filters.update({'instance-state-name': state})

    if zone:
        filters.update({'availability-zone': zone})

    results = ec2.get_all_instances(filters=filters)

    return results

def get_instance_info(inst):
    """
    Retrieves instance information from an instance
    ID and returns it as a dictionary
    """
    instance_info = {'id': inst.id,
                     'ami_launch_index': inst.ami_launch_index,
                     'private_ip': inst.private_ip_address,
                     'private_dns_name': inst.private_dns_name,
                     'public_ip': inst.ip_address,
                     'dns_name': inst.dns_name,
                     'public_dns_name': inst.public_dns_name,
                     'state_code': inst.state_code,
                     'architecture': inst.architecture,
                     'image_id': inst.image_id,
                     'key_name': inst.key_name,
                     'placement': inst.placement,
                     'region': inst.placement[:-1],
                     'kernel': inst.kernel,
                     'ramdisk': inst.ramdisk,
                     'launch_time': inst.launch_time,
                     'instance_type': inst.instance_type,
                     'root_device_type': inst.root_device_type,
                     'root_device_name': inst.root_device_name,
                     'state': inst.state,
                     'hypervisor': inst.hypervisor,
                     'tags': inst.tags,
                     'groups': dict((group.id, group.name) for group in inst.groups),
                     }
    try:
        instance_info['virtualization_type'] = getattr(inst,'virtualization_type')
    except AttributeError:
        instance_info['virtualization_type'] = None

    try:
        instance_info['ebs_optimized'] = getattr(inst, 'ebs_optimized')
    except AttributeError:
        instance_info['ebs_optimized'] = False

    try:
        bdm_dict = {}
        bdm = getattr(inst, 'block_device_mapping')
        for device_name in bdm.keys():
            bdm_dict[device_name] = {
                'status': bdm[device_name].status,
                'volume_id': bdm[device_name].volume_id,
                'delete_on_termination': bdm[device_name].delete_on_termination
            }
        instance_info['block_device_mapping'] = bdm_dict
    except AttributeError:
        instance_info['block_device_mapping'] = False

    try:
        instance_info['tenancy'] = getattr(inst, 'placement_tenancy')
    except AttributeError:
        instance_info['tenancy'] = 'default'

    return instance_info

def boto_supports_associate_public_ip_address(ec2):
    """
    Check if Boto library has associate_public_ip_address in the NetworkInterfaceSpecification
    class. Added in Boto 2.13.0
    ec2: authenticated ec2 connection object
    Returns:
        True if Boto library accepts associate_public_ip_address argument, else false
    """

    try:
        network_interface = boto.ec2.networkinterface.NetworkInterfaceSpecification()
        getattr(network_interface, "associate_public_ip_address")
        return True
    except AttributeError:
        return False

def boto_supports_profile_name_arg(ec2):
    """
    Check if Boto library has instance_profile_name argument. instance_profile_name has been added in Boto 2.5.0
    ec2: authenticated ec2 connection object
    Returns:
        True if Boto library accept instance_profile_name argument, else false
    """
    run_instances_method = getattr(ec2, 'run_instances')
    return 'instance_profile_name' in get_function_code(run_instances_method).co_varnames

def create_block_device(module, ec2, volume):
    # Not aware of a way to determine this programatically
    # http://aws.amazon.com/about-aws/whats-new/2013/10/09/ebs-provisioned-iops-maximum-iops-gb-ratio-increased-to-30-1/
    MAX_IOPS_TO_SIZE_RATIO = 30

    # device_type has been used historically to represent volume_type,
    # however ec2_vol uses volume_type, as does the BlockDeviceType, so
    # we add handling for either/or but not both
    if all(key in volume for key in ['device_type','volume_type']):
        module.fail_json(msg = 'device_type is a deprecated name for volume_type. Do not use both device_type and volume_type')

    # get whichever one is set, or NoneType if neither are set
    volume_type = volume.get('device_type') or volume.get('volume_type')

    if 'snapshot' not in volume and 'ephemeral' not in volume:
        if 'volume_size' not in volume:
            module.fail_json(msg = 'Size must be specified when creating a new volume or modifying the root volume')
    if 'snapshot' in volume:
        if volume_type == 'io1' and 'iops' not in volume:
            module.fail_json(msg = 'io1 volumes must have an iops value set')
        if 'iops' in volume:
            snapshot = ec2.get_all_snapshots(snapshot_ids=[volume['snapshot']])[0]
            size = volume.get('volume_size', snapshot.volume_size)
            if int(volume['iops']) > MAX_IOPS_TO_SIZE_RATIO * size:
                module.fail_json(msg = 'IOPS must be at most %d times greater than size' % MAX_IOPS_TO_SIZE_RATIO)
        if 'encrypted' in volume:
            module.fail_json(msg = 'You can not set encryption when creating a volume from a snapshot')
    if 'ephemeral' in volume:
        if 'snapshot' in volume:
            module.fail_json(msg = 'Cannot set both ephemeral and snapshot')
    return BlockDeviceType(snapshot_id=volume.get('snapshot'),
                           ephemeral_name=volume.get('ephemeral'),
                           size=volume.get('volume_size'),
                           volume_type=volume_type,
                           delete_on_termination=volume.get('delete_on_termination', False),
                           iops=volume.get('iops'),
                           encrypted=volume.get('encrypted', None))

def boto_supports_param_in_spot_request(ec2, param):
    """
    Check if Boto library has a <param> in its request_spot_instances() method. For example, the placement_group parameter wasn't added until 2.3.0.
    ec2: authenticated ec2 connection object
    Returns:
        True if boto library has the named param as an argument on the request_spot_instances method, else False
    """
    method = getattr(ec2, 'request_spot_instances')
    return param in get_function_code(method).co_varnames

def await_spot_requests(module, ec2, spot_requests, count):
    """
    Wait for a group of spot requests to be fulfilled, or fail.
    module: Ansible module object
    ec2: authenticated ec2 connection object
    spot_requests: boto.ec2.spotinstancerequest.SpotInstanceRequest object returned by ec2.request_spot_instances
    count: Total number of instances to be created by the spot requests
    Returns:
        list of instance ID's created by the spot request(s)
    """
    spot_wait_timeout = int(module.params.get('spot_wait_timeout'))
    wait_complete = time.time() + spot_wait_timeout

    spot_req_inst_ids = dict()
    while time.time() < wait_complete:
        reqs = ec2.get_all_spot_instance_requests()
        for sirb in spot_requests:
            if sirb.id in spot_req_inst_ids:
                continue
            for sir in reqs:
                if sir.id != sirb.id:
                    continue # this is not our spot instance
                if sir.instance_id is not None:
                    spot_req_inst_ids[sirb.id] = sir.instance_id
                elif sir.state == 'open':
                    continue # still waiting, nothing to do here
                elif sir.state == 'active':
                    continue # Instance is created already, nothing to do here
                elif sir.state == 'failed':
                    module.fail_json(msg="Spot instance request %s failed with status %s and fault %s:%s" % (
                        sir.id, sir.status.code, sir.fault.code, sir.fault.message))
                elif sir.state == 'cancelled':
                    module.fail_json(msg="Spot instance request %s was cancelled before it could be fulfilled." % sir.id)
                elif sir.state == 'closed':
                    # instance is terminating or marked for termination
                    # this may be intentional on the part of the operator,
                    # or it may have been terminated by AWS due to capacity,
                    # price, or group constraints in this case, we'll fail
                    # the module if the reason for the state is anything
                    # other than termination by user. Codes are documented at
                    # http://docs.aws.amazon.com/AWSEC2/latest/UserGuide/spot-bid-status.html
                    if sir.status.code == 'instance-terminated-by-user':
                        # do nothing, since the user likely did this on purpose
                        pass
                    else:
                        spot_msg = "Spot instance request %s was closed by AWS with the status %s and fault %s:%s"
                        module.fail_json(msg=spot_msg % (sir.id, sir.status.code, sir.fault.code, sir.fault.message))

        if len(spot_req_inst_ids) < count:
            time.sleep(5)
        else:
            return spot_req_inst_ids.values()
    module.fail_json(msg = "wait for spot requests timeout on %s" % time.asctime())


def enforce_count(module, ec2, vpc):

    exact_count = module.params.get('exact_count')
    count_tag = module.params.get('count_tag')
    zone = module.params.get('zone')

    # fail here if the exact count was specified without filtering
    # on a tag, as this may lead to a undesired removal of instances
    if exact_count and count_tag is None:
        module.fail_json(msg="you must use the 'count_tag' option with exact_count")

    reservations, instances = find_running_instances_by_count_tag(module, ec2, count_tag, zone)

    changed = None
    checkmode = False
    instance_dict_array = []
    changed_instance_ids = None

    if len(instances) == exact_count:
        changed = False
    elif len(instances) < exact_count:
        changed = True
        to_create = exact_count - len(instances)
        if not checkmode:
            (instance_dict_array, changed_instance_ids, changed) \
                = create_instances(module, ec2, vpc, override_count=to_create)

            for inst in instance_dict_array:
                instances.append(inst)
    elif len(instances) > exact_count:
        changed = True
        to_remove = len(instances) - exact_count
        if not checkmode:
            all_instance_ids = sorted([ x.id for x in instances ])
            remove_ids = all_instance_ids[0:to_remove]

            instances = [ x for x in instances if x.id not in remove_ids]

            (changed, instance_dict_array, changed_instance_ids) \
                = terminate_instances(module, ec2, remove_ids)
            terminated_list = []
            for inst in instance_dict_array:
                inst['state'] = "terminated"
                terminated_list.append(inst)
            instance_dict_array = terminated_list

    # ensure all instances are dictionaries
    all_instances = []
    for inst in instances:
        if not isinstance(inst, dict):
            inst = get_instance_info(inst)
        all_instances.append(inst)

    return (all_instances, instance_dict_array, changed_instance_ids, changed)


def create_instances(module, ec2, vpc, override_count=None):
    """
    Creates new instances
    module : AnsibleModule object
    ec2: authenticated ec2 connection object
    Returns:
        A list of dictionaries with instance information
        about the instances that were launched
    """

    key_name = module.params.get('key_name')
    id = module.params.get('id')
    group_name = module.params.get('group')
    group_id = module.params.get('group_id')
    zone = module.params.get('zone')
    instance_type = module.params.get('instance_type')
    tenancy = module.params.get('tenancy')
    spot_price = module.params.get('spot_price')
    spot_type = module.params.get('spot_type')
    image = module.params.get('image')
    if override_count:
        count = override_count
    else:
        count = module.params.get('count')
    monitoring = module.params.get('monitoring')
    kernel = module.params.get('kernel')
    ramdisk = module.params.get('ramdisk')
    wait = module.params.get('wait')
    wait_timeout = int(module.params.get('wait_timeout'))
    spot_wait_timeout = int(module.params.get('spot_wait_timeout'))
    placement_group = module.params.get('placement_group')
    user_data = module.params.get('user_data')
    instance_tags = module.params.get('instance_tags')
    vpc_subnet_id = module.params.get('vpc_subnet_id')
    assign_public_ip = module.boolean(module.params.get('assign_public_ip'))
    private_ip = module.params.get('private_ip')
    instance_profile_name = module.params.get('instance_profile_name')
    volumes = module.params.get('volumes')
    ebs_optimized = module.params.get('ebs_optimized')
    exact_count = module.params.get('exact_count')
    count_tag = module.params.get('count_tag')
    source_dest_check = module.boolean(module.params.get('source_dest_check'))
    termination_protection = module.boolean(module.params.get('termination_protection'))
    network_interfaces = module.params.get('network_interfaces')
    spot_launch_group = module.params.get('spot_launch_group')
    instance_initiated_shutdown_behavior = module.params.get('instance_initiated_shutdown_behavior')

    # group_id and group_name are exclusive of each other
    if group_id and group_name:
        module.fail_json(msg = str("Use only one type of parameter (group_name) or (group_id)"))

    vpc_id = None
    if vpc_subnet_id:
        if not vpc:
            module.fail_json(msg="region must be specified")
        else:
            vpc_id = vpc.get_all_subnets(subnet_ids=[vpc_subnet_id])[0].vpc_id
    else:
        vpc_id = None

    try:
        # Here we try to lookup the group id from the security group name - if group is set.
        if group_name:
            if vpc_id:
                grp_details = ec2.get_all_security_groups(filters={'vpc_id': vpc_id})
            else:
                grp_details = ec2.get_all_security_groups()
            if isinstance(group_name, basestring):
                group_name = [group_name]
            unmatched = set(group_name).difference(str(grp.name) for grp in grp_details)
            if len(unmatched) > 0:
                module.fail_json(msg="The following group names are not valid: %s" % ', '.join(unmatched))
            group_id = [ str(grp.id) for grp in grp_details if str(grp.name) in group_name ]
        # Now we try to lookup the group id testing if group exists.
        elif group_id:
            #wrap the group_id in a list if it's not one already
            if isinstance(group_id, basestring):
                group_id = [group_id]
            grp_details = ec2.get_all_security_groups(group_ids=group_id)
            group_name = [grp_item.name for grp_item in grp_details]
    except boto.exception.NoAuthHandlerFound as e:
            module.fail_json(msg = str(e))

    # Lookup any instances that much our run id.

    running_instances = []
    count_remaining = int(count)

    if id != None:
        filter_dict = {'client-token':id, 'instance-state-name' : 'running'}
        previous_reservations = ec2.get_all_instances(None, filter_dict)
        for res in previous_reservations:
            for prev_instance in res.instances:
                running_instances.append(prev_instance)
        count_remaining = count_remaining - len(running_instances)

    # Both min_count and max_count equal count parameter. This means the launch request is explicit (we want count, or fail) in how many instances we want.

    if count_remaining == 0:
        changed = False
    else:
        changed = True
        try:
            params = {'image_id': image,
                      'key_name': key_name,
                      'monitoring_enabled': monitoring,
                      'placement': zone,
                      'instance_type': instance_type,
                      'kernel_id': kernel,
                      'ramdisk_id': ramdisk,
                      'user_data': user_data}

            if ebs_optimized:
              params['ebs_optimized'] = ebs_optimized

            # 'tenancy' always has a default value, but it is not a valid parameter for spot instance request
            if not spot_price:
              params['tenancy'] = tenancy

            if boto_supports_profile_name_arg(ec2):
                params['instance_profile_name'] = instance_profile_name
            else:
                if instance_profile_name is not None:
                    module.fail_json(
                        msg="instance_profile_name parameter requires Boto version 2.5.0 or higher")

            if assign_public_ip:
                if not boto_supports_associate_public_ip_address(ec2):
                    module.fail_json(
                        msg="assign_public_ip parameter requires Boto version 2.13.0 or higher.")
                elif not vpc_subnet_id:
                    module.fail_json(
                        msg="assign_public_ip only available with vpc_subnet_id")

                else:
                    if private_ip:
                        interface = boto.ec2.networkinterface.NetworkInterfaceSpecification(
                            subnet_id=vpc_subnet_id,
                            private_ip_address=private_ip,
                            groups=group_id,
                            associate_public_ip_address=assign_public_ip)
                    else:
                        interface = boto.ec2.networkinterface.NetworkInterfaceSpecification(
                            subnet_id=vpc_subnet_id,
                            groups=group_id,
                            associate_public_ip_address=assign_public_ip)
                    interfaces = boto.ec2.networkinterface.NetworkInterfaceCollection(interface)
                    params['network_interfaces'] = interfaces
            else:
                if network_interfaces:
                    if isinstance(network_interfaces, basestring):
                        network_interfaces = [network_interfaces]
                    interfaces = []
                    for i, network_interface_id in enumerate(network_interfaces):
                        interface = boto.ec2.networkinterface.NetworkInterfaceSpecification(
                            network_interface_id=network_interface_id,
                            device_index=i)
                        interfaces.append(interface)
                    params['network_interfaces'] = \
                        boto.ec2.networkinterface.NetworkInterfaceCollection(*interfaces)
                else:
                    params['subnet_id'] = vpc_subnet_id
                    if vpc_subnet_id:
                        params['security_group_ids'] = group_id
                    else:
                        params['security_groups'] = group_name

            if volumes:
                bdm = BlockDeviceMapping()
                for volume in volumes:
                    if 'device_name' not in volume:
                        module.fail_json(msg = 'Device name must be set for volume')
                    # Minimum volume size is 1GB. We'll use volume size explicitly set to 0
                    # to be a signal not to create this volume
                    if 'volume_size' not in volume or int(volume['volume_size']) > 0:
                        bdm[volume['device_name']] = create_block_device(module, ec2, volume)

                params['block_device_map'] = bdm

            # check to see if we're using spot pricing first before starting instances
            if not spot_price:
                if assign_public_ip and private_ip:
                    params.update(dict(
                      min_count          = count_remaining,
                      max_count          = count_remaining,
                      client_token       = id,
                      placement_group    = placement_group,
                    ))
                else:
                    params.update(dict(
                      min_count          = count_remaining,
                      max_count          = count_remaining,
                      client_token       = id,
                      placement_group    = placement_group,
                      private_ip_address = private_ip,
                    ))

                # For ordinary (not spot) instances, we can select 'stop'
                # (the default) or 'terminate' here.
                params['instance_initiated_shutdown_behavior'] = instance_initiated_shutdown_behavior or 'stop'

                res = ec2.run_instances(**params)
                instids = [ i.id for i in res.instances ]
                while True:
                    try:
                        ec2.get_all_instances(instids)
                        break
                    except boto.exception.EC2ResponseError as e:
                        if "<Code>InvalidInstanceID.NotFound</Code>" in str(e):
                            # there's a race between start and get an instance
                            continue
                        else:
                            module.fail_json(msg = str(e))

                # The instances returned through ec2.run_instances above can be in
                # terminated state due to idempotency. See commit 7f11c3d for a complete
                # explanation.
                terminated_instances = [
                    str(instance.id) for instance in res.instances if instance.state == 'terminated'
                ]
                if terminated_instances:
                    module.fail_json(msg = "Instances with id(s) %s " % terminated_instances +
                                           "were created previously but have since been terminated - " +
                                           "use a (possibly different) 'instanceid' parameter")

            else:
                if private_ip:
                    module.fail_json(
                        msg='private_ip only available with on-demand (non-spot) instances')
                if boto_supports_param_in_spot_request(ec2, 'placement_group'):
                    params['placement_group'] = placement_group
                elif placement_group :
                        module.fail_json(
                            msg="placement_group parameter requires Boto version 2.3.0 or higher.")

                # You can't tell spot instances to 'stop'; they will always be
                # 'terminate'd. For convenience, we'll ignore the latter value.
                if instance_initiated_shutdown_behavior and instance_initiated_shutdown_behavior != 'terminate':
                    module.fail_json(
                        msg="instance_initiated_shutdown_behavior=stop is not supported for spot instances.")

                if spot_launch_group and isinstance(spot_launch_group, basestring):
                    params['launch_group'] = spot_launch_group

                params.update(dict(
                    count = count_remaining,
                    type = spot_type,
                ))
                res = ec2.request_spot_instances(spot_price, **params)

                # Now we have to do the intermediate waiting
                if wait:
                    instids = await_spot_requests(module, ec2, res, count)
        except boto.exception.BotoServerError as e:
            module.fail_json(msg = "Instance creation failed => %s: %s" % (e.error_code, e.error_message))

        # wait here until the instances are up
        num_running = 0
        wait_timeout = time.time() + wait_timeout
        while wait_timeout > time.time() and num_running < len(instids):
            try:
                res_list = ec2.get_all_instances(instids)
            except boto.exception.BotoServerError as e:
                if e.error_code == 'InvalidInstanceID.NotFound':
                    time.sleep(1)
                    continue
                else:
                    raise

            num_running = 0
            for res in res_list:
                num_running += len([ i for i in res.instances if i.state=='running' ])
            if len(res_list) <= 0:
                # got a bad response of some sort, possibly due to
                # stale/cached data. Wait a second and then try again
                time.sleep(1)
                continue
            if wait and num_running < len(instids):
                time.sleep(5)
            else:
                break

        if wait and wait_timeout <= time.time():
            # waiting took too long
            module.fail_json(msg = "wait for instances running timeout on %s" % time.asctime())

        #We do this after the loop ends so that we end up with one list
        for res in res_list:
            running_instances.extend(res.instances)

        # Enabled by default by AWS
        if source_dest_check is False:
            for inst in res.instances:
                inst.modify_attribute('sourceDestCheck', False)

        # Disabled by default by AWS
        if termination_protection is True:
            for inst in res.instances:
                inst.modify_attribute('disableApiTermination', True)

        # Leave this as late as possible to try and avoid InvalidInstanceID.NotFound
        if instance_tags:
            try:
                ec2.create_tags(instids, instance_tags)
            except boto.exception.EC2ResponseError as e:
                module.fail_json(msg = "Instance tagging failed => %s: %s" % (e.error_code, e.error_message))

    instance_dict_array = []
    created_instance_ids = []
    for inst in running_instances:
        inst.update()
        d = get_instance_info(inst)
        created_instance_ids.append(inst.id)
        instance_dict_array.append(d)

    return (instance_dict_array, created_instance_ids, changed)


def terminate_instances(module, ec2, instance_ids):
    """
    Terminates a list of instances
    module: Ansible module object
    ec2: authenticated ec2 connection object
    termination_list: a list of instances to terminate in the form of
      [ {id: <inst-id>}, ..]
    Returns a dictionary of instance information
    about the instances terminated.
    If the instance to be terminated is running
    "changed" will be set to False.
    """

    # Whether to wait for termination to complete before returning
    wait = module.params.get('wait')
    wait_timeout = int(module.params.get('wait_timeout'))

    changed = False
    instance_dict_array = []

    if not isinstance(instance_ids, list) or len(instance_ids) < 1:
        module.fail_json(msg='instance_ids should be a list of instances, aborting')

    terminated_instance_ids = []
    for res in ec2.get_all_instances(instance_ids):
        for inst in res.instances:
            if inst.state == 'running' or inst.state == 'stopped':
                terminated_instance_ids.append(inst.id)
                instance_dict_array.append(get_instance_info(inst))
                try:
                    ec2.terminate_instances([inst.id])
                except EC2ResponseError as e:
                    module.fail_json(msg='Unable to terminate instance {0}, error: {1}'.format(inst.id, e))
                changed = True

    # wait here until the instances are 'terminated'
    if wait:
        num_terminated = 0
        wait_timeout = time.time() + wait_timeout
        while wait_timeout > time.time() and num_terminated < len(terminated_instance_ids):
            response = ec2.get_all_instances( \
                instance_ids=terminated_instance_ids, \
                filters={'instance-state-name':'terminated'})
            try:
                num_terminated = sum([len(res.instances) for res in response])
            except Exception as e:
                # got a bad response of some sort, possibly due to
                # stale/cached data. Wait a second and then try again
                time.sleep(1)
                continue

            if num_terminated < len(terminated_instance_ids):
                time.sleep(5)

        # waiting took too long
        if wait_timeout < time.time() and num_terminated < len(terminated_instance_ids):
            module.fail_json(msg = "wait for instance termination timeout on %s" % time.asctime())
        #Lets get the current state of the instances after terminating - issue600
        instance_dict_array = []
        for res in ec2.get_all_instances(instance_ids=terminated_instance_ids,\
                                            filters={'instance-state-name':'terminated'}):
            for inst in res.instances:
                instance_dict_array.append(get_instance_info(inst))


    return (changed, instance_dict_array, terminated_instance_ids)


def startstop_instances(module, ec2, instance_ids, state, instance_tags):
    """
    Starts or stops a list of existing instances
    module: Ansible module object
    ec2: authenticated ec2 connection object
    instance_ids: The list of instances to start in the form of
      [ {id: <inst-id>}, ..]
    instance_tags: A dict of tag keys and values in the form of
      {key: value, ... }
    state: Intended state ("running" or "stopped")
    Returns a dictionary of instance information
    about the instances started/stopped.
    If the instance was not able to change state,
    "changed" will be set to False.
    Note that if instance_ids and instance_tags are both non-empty,
    this method will process the intersection of the two
    """

    wait = module.params.get('wait')
    wait_timeout = int(module.params.get('wait_timeout'))
    source_dest_check = module.params.get('source_dest_check')
    termination_protection = module.params.get('termination_protection')
    changed = False
    instance_dict_array = []

    if not isinstance(instance_ids, list) or len(instance_ids) < 1:
        # Fail unless the user defined instance tags
        if not instance_tags:
            module.fail_json(msg='instance_ids should be a list of instances, aborting')

    # To make an EC2 tag filter, we need to prepend 'tag:' to each key.
    # An empty filter does no filtering, so it's safe to pass it to the
    # get_all_instances method even if the user did not specify instance_tags
    filters = {}
    if instance_tags:
        for key, value in instance_tags.items():
            filters["tag:" + key] = value

     # Check that our instances are not in the state we want to take

    # Check (and eventually change) instances attributes and instances state
    existing_instances_array = []
    for res in ec2.get_all_instances(instance_ids, filters=filters):
        for inst in res.instances:

            # Check "source_dest_check" attribute
            try:
                if inst.vpc_id is not None and inst.get_attribute('sourceDestCheck')['sourceDestCheck'] != source_dest_check:
                    inst.modify_attribute('sourceDestCheck', source_dest_check)
                    changed = True
            except boto.exception.EC2ResponseError as exc:
                # instances with more than one Elastic Network Interface will
                # fail, because they have the sourceDestCheck attribute defined
                # per-interface
                if exc.code == 'InvalidInstanceID':
                    for interface in inst.interfaces:
                        if interface.source_dest_check != source_dest_check:
                            ec2.modify_network_interface_attribute(interface.id, "sourceDestCheck", source_dest_check)
                            changed = True
                else:
                    module.fail_json(msg='Failed to handle source_dest_check state for instance {0}, error: {1}'.format(inst.id, exc),
                                     exception=traceback.format_exc(exc))

            # Check "termination_protection" attribute
            if (inst.get_attribute('disableApiTermination')['disableApiTermination'] != termination_protection
                    and termination_protection is not None):
                inst.modify_attribute('disableApiTermination', termination_protection)
                changed = True

            # Check instance state
            if inst.state != state:
                instance_dict_array.append(get_instance_info(inst))
                try:
                    if state == 'running':
                        inst.start()
                    else:
                        inst.stop()
                except EC2ResponseError as e:
                    module.fail_json(msg='Unable to change state for instance {0}, error: {1}'.format(inst.id, e))
                changed = True
            existing_instances_array.append(inst.id)

    instance_ids = list(set(existing_instances_array + (instance_ids or [])))
    ## Wait for all the instances to finish starting or stopping
    wait_timeout = time.time() + wait_timeout
    while wait and wait_timeout > time.time():
        instance_dict_array = []
        matched_instances = []
        for res in ec2.get_all_instances(instance_ids):
            for i in res.instances:
                if i.state == state:
                    instance_dict_array.append(get_instance_info(i))
                    matched_instances.append(i)
        if len(matched_instances) < len(instance_ids):
            time.sleep(5)
        else:
            break

    if wait and wait_timeout <= time.time():
        # waiting took too long
        module.fail_json(msg = "wait for instances running timeout on %s" % time.asctime())

    return (changed, instance_dict_array, instance_ids)

def restart_instances(module, ec2, instance_ids, state, instance_tags):
    """
    Restarts a list of existing instances
    module: Ansible module object
    ec2: authenticated ec2 connection object
    instance_ids: The list of instances to start in the form of
      [ {id: <inst-id>}, ..]
    instance_tags: A dict of tag keys and values in the form of
      {key: value, ... }
    state: Intended state ("restarted")
    Returns a dictionary of instance information
    about the instances.
    If the instance was not able to change state,
    "changed" will be set to False.
    Wait will not apply here as this is a OS level operation.
    Note that if instance_ids and instance_tags are both non-empty,
    this method will process the intersection of the two.
    """

    source_dest_check = module.params.get('source_dest_check')
    termination_protection = module.params.get('termination_protection')
    changed = False
    instance_dict_array = []

    if not isinstance(instance_ids, list) or len(instance_ids) < 1:
        # Fail unless the user defined instance tags
        if not instance_tags:
            module.fail_json(msg='instance_ids should be a list of instances, aborting')

    # To make an EC2 tag filter, we need to prepend 'tag:' to each key.
    # An empty filter does no filtering, so it's safe to pass it to the
    # get_all_instances method even if the user did not specify instance_tags
    filters = {}
    if instance_tags:
        for key, value in instance_tags.items():
            filters["tag:" + key] = value

     # Check that our instances are not in the state we want to take

    # Check (and eventually change) instances attributes and instances state
    for res in ec2.get_all_instances(instance_ids, filters=filters):
        for inst in res.instances:

            # Check "source_dest_check" attribute
            try:
                if inst.vpc_id is not None and inst.get_attribute('sourceDestCheck')['sourceDestCheck'] != source_dest_check:
                    inst.modify_attribute('sourceDestCheck', source_dest_check)
                    changed = True
            except boto.exception.EC2ResponseError as exc:
                # instances with more than one Elastic Network Interface will
                # fail, because they have the sourceDestCheck attribute defined
                # per-interface
                if exc.code == 'InvalidInstanceID':
                    for interface in inst.interfaces:
                        if interface.source_dest_check != source_dest_check:
                            ec2.modify_network_interface_attribute(interface.id, "sourceDestCheck", source_dest_check)
                            changed = True
                else:
                    module.fail_json(msg='Failed to handle source_dest_check state for instance {0}, error: {1}'.format(inst.id, exc),
                                     exception=traceback.format_exc(exc))

            # Check "termination_protection" attribute
            if (inst.get_attribute('disableApiTermination')['disableApiTermination'] != termination_protection
                    and termination_protection is not None):
                inst.modify_attribute('disableApiTermination', termination_protection)
                changed = True

            # Check instance state
            if inst.state != state:
                instance_dict_array.append(get_instance_info(inst))
                try:
                    inst.reboot()
                except EC2ResponseError as e:
                    module.fail_json(msg='Unable to change state for instance {0}, error: {1}'.format(inst.id, e))
                changed = True

    return (changed, instance_dict_array, instance_ids)


def main():
    argument_spec = ec2_argument_spec()
    argument_spec.update(dict(
            key_name = dict(aliases = ['keypair']),
            id = dict(),
            group = dict(type='list', aliases=['groups']),
            group_id = dict(type='list'),
            zone = dict(aliases=['aws_zone', 'ec2_zone']),
            instance_type = dict(aliases=['type']),
            spot_price = dict(),
            spot_type = dict(default='one-time', choices=["one-time", "persistent"]),
            spot_launch_group = dict(),
            image = dict(),
            kernel = dict(),
            count = dict(type='int', default='1'),
            monitoring = dict(type='bool', default=False),
            ramdisk = dict(),
            wait = dict(type='bool', default=False),
            wait_timeout = dict(default=300),
            spot_wait_timeout = dict(default=600),
            placement_group = dict(),
            user_data = dict(),
            instance_tags = dict(type='dict'),
            vpc_subnet_id = dict(),
            assign_public_ip = dict(type='bool', default=False),
            private_ip = dict(),
            instance_profile_name = dict(),
            instance_ids = dict(type='list', aliases=['instance_id']),
            source_dest_check = dict(type='bool', default=True),
            termination_protection = dict(type='bool', default=None),
            state = dict(default='present', choices=['present', 'absent', 'running', 'restarted', 'stopped']),
            instance_initiated_shutdown_behavior=dict(default=None, choices=['stop', 'terminate']),
            exact_count = dict(type='int', default=None),
            count_tag = dict(),
            volumes = dict(type='list'),
            ebs_optimized = dict(type='bool', default=False),
            tenancy = dict(default='default'),
            network_interfaces = dict(type='list', aliases=['network_interface'])
        )
    )

    module = AnsibleModule(
        argument_spec=argument_spec,
        mutually_exclusive = [
                                ['exact_count', 'count'],
                                ['exact_count', 'state'],
                                ['exact_count', 'instance_ids'],
                                ['network_interfaces', 'assign_public_ip'],
                                ['network_interfaces', 'group'],
                                ['network_interfaces', 'group_id'],
                                ['network_interfaces', 'private_ip'],
                                ['network_interfaces', 'vpc_subnet_id'],
                             ],
    )

    if not HAS_BOTO:
        module.fail_json(msg='boto required for this module')

    ec2 = ec2_connect(module)

    region, ec2_url, aws_connect_kwargs = get_aws_connection_info(module)

    if region:
        try:
            vpc = connect_to_aws(boto.vpc, region, **aws_connect_kwargs)
        except boto.exception.NoAuthHandlerFound as e:
            module.fail_json(msg = str(e))
    else:
        vpc = None

    tagged_instances = []

    state = module.params['state']

    if state == 'absent':
        instance_ids = module.params['instance_ids']
        if not instance_ids:
            module.fail_json(msg='instance_ids list is required for absent state')

        (changed, instance_dict_array, new_instance_ids) = terminate_instances(module, ec2, instance_ids)

    elif state in ('running', 'stopped'):
        instance_ids = module.params.get('instance_ids')
        instance_tags = module.params.get('instance_tags')
        if not (isinstance(instance_ids, list) or isinstance(instance_tags, dict)):
            module.fail_json(msg='running list needs to be a list of instances or set of tags to run: %s' % instance_ids)

        (changed, instance_dict_array, new_instance_ids) = startstop_instances(module, ec2, instance_ids, state, instance_tags)

    elif state in ('restarted'):
        instance_ids = module.params.get('instance_ids')
        instance_tags = module.params.get('instance_tags')
        if not (isinstance(instance_ids, list) or isinstance(instance_tags, dict)):
            module.fail_json(msg='running list needs to be a list of instances or set of tags to run: %s' % instance_ids)

        (changed, instance_dict_array, new_instance_ids) = restart_instances(module, ec2, instance_ids, state, instance_tags)

    elif state == 'present':
        # Changed is always set to true when provisioning new instances
        if not module.params.get('image'):
            module.fail_json(msg='image parameter is required for new instance')

        if module.params.get('exact_count') is None:
            (instance_dict_array, new_instance_ids, changed) = create_instances(module, ec2, vpc)
        else:
            (tagged_instances, instance_dict_array, new_instance_ids, changed) = enforce_count(module, ec2, vpc)

    module.exit_json(changed=changed, instance_ids=new_instance_ids, instances=instance_dict_array, tagged_instances=tagged_instances)

# import module snippets
from ansible.module_utils.basic import *
from ansible.module_utils.ec2 import *

if __name__ == '__main__':
    main()

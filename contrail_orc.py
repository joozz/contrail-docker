#!/usr/bin/env python

import sys
import os
import re
sys.path.insert(0,"/opt/contrail/utils")
from fabric.tasks import execute
from fabric.main import load_fabfile
from fabric import state
from fabric.api import run, local, settings
from fabfile.tasks.install import *
from fabfile.tasks.provision import *
from fabfile.utils.commandline import *
from fabric.contrib.files import exists, append
import argparse
import tempfile

@task
@parallel
@roles('cfgm')
def install_docker():
    """ At this moment, only ubuntu is supported
    """
    ostype = detect_ostype()
    if ostype in ['ubuntu']:
        cmd = "apt-key adv --keyserver hkp://p80.pool.sks-keyservers.net:80 --recv-keys 58118E89F3A912897C070ADBF76221572C52609D; "
        cmd += "echo 'deb https://apt.dockerproject.org/repo ubuntu-trusty main' > /etc/apt/sources.list.d/docker.list; "
        cmd += "apt-get -q update; "
        cmd += "DEBIAN_FRONTEND=noninteractive apt-get install -q -y --force-yes docker-engine; "
        sudo(cmd)
    else:
        print("Unsupported Operating system - %s" % ostype)


@task
@parallel
def load_docker_image(image_url):
    image_file = os.path.basename(image_url)
    cmd = "wget -q -O /tmp/%s %s; " % (image_file, image_url)
    cmd += "docker load -q -i /tmp/%s" % (image_file)
    run(cmd)


def add_keystone_cmd_params():
    authserver_ip = get_authserver_ip()
    (_, openstack_admin_password) = get_authserver_credentials()
    cmd = " -e ADMIN_TOKEN=%s" % get_service_token()
    cmd += " -e KEYSTONE_SERVER=%s" % authserver_ip
    cmd += " -e OS_PASSWORD=%s" % openstack_admin_password
    cmd += " -e SERVICE_TENANT=%s" % get_keystone_service_tenant_name()
    cmd += ' -e NEUTRON_PASSWORD=%s' % get_neutron_password()
    cmd += " -e KEYSTONE_AUTH_PROTOCOL=%s" % get_authserver_protocol()
    cmd += " -e KEYSTONE_AUTH_PORT=%s" % get_authserver_port()
    cmd += " -e KEYSTONE_INSECURE=%s" % get_keystone_insecure_flag()
    cmd += " -e REGION=%s" % get_region_name()
    return cmd


def frame_analytics_docker_cmd(host_string, contrail_version, openstack_sku):
    cfgm_host = get_control_host_string(env.roledefs['cfgm'][0])
    cfgm_ip = get_contrail_internal_vip() or hstr_to_ip(cfgm_host)
    collector_host_password = get_env_passwords(host_string)
    collector_host = get_control_host_string(host_string)
    ncollectors = len(env.roledefs['collector'])
    redis_master_host = get_control_host_string(env.roledefs['collector'][0])
    tgt_ip = hstr_to_ip(collector_host)
    database_host_list = [get_control_host_string(database_host) for database_host in env.roledefs['database']]
    if collector_host in database_host_list:
        database_host_list.remove(collector_host)
        database_host_list.insert(0, collector_host)
    cassandra_ip_list = [hstr_to_ip(cassandra_host) for cassandra_host in database_host_list]
    zookeeper_ip_list = [hstr_to_ip(zookeeper_host) for zookeeper_host in database_host_list]
    redis_master_ip = hstr_to_ip(redis_master_host)
    cassandra_user = get_cassandra_user()
    cassandra_password = get_cassandra_password()

    cmd = "docker run --net=host --restart=unless-stopped --name contrail-analytics"

    # Frame the command line to provision collector
    cmd += " -e CASSANDRA_SERVER_LIST=%s" % (','.join(cassandra_ip_list))
    cmd += " -e ZOOKEEPER_SERVER_LIST=\"%s\"" % (','.join(zookeeper_ip_list))
    cmd += " -e CONFIG_IP=%s" % cfgm_ip
    cmd += " -e REDIS_SERVER=%s" % redis_master_ip
    analytics_syslog_port = get_collector_syslog_port()
    if analytics_syslog_port is not None:
        cmd += "-e ANALYTICS_SYSLOG_PORT=%d " % analytics_syslog_port
    analytics_database_ttl = get_database_ttl()
    if analytics_database_ttl is not None:
        cmd += " -e ANALYTICS_DATA_TTL=%d " % analytics_database_ttl
    analytics_config_audit_ttl = get_analytics_config_audit_ttl()
    if analytics_config_audit_ttl is not None:
        cmd += " -e ANALYTICS_CONFIG_AUDIT_TTL=%d " % analytics_config_audit_ttl
    analytics_statistics_ttl = get_analytics_statistics_ttl()
    if analytics_statistics_ttl is not None:
        cmd += " -e ANALYTICS_STATISTICS_TTL=%d " % analytics_statistics_ttl
    analytics_flow_ttl = get_analytics_flow_ttl()
    if analytics_flow_ttl is not None:
        cmd += " -e ANALYTICS_FLOW_TTL=%d " % analytics_flow_ttl
    analytics_redis_password = get_redis_password()
    # BELOW THINGS TO BE DONE
#    if analytics_redis_password is not None:
#        cmd += "--redis_password %s " % analytics_redis_password
    if get_kafka_enabled():
        cmd += " -e KAFKA_ENABLED=1"
    cmd += add_keystone_cmd_params()
#    internal_vip = get_contrail_internal_vip()
#    if internal_vip:
#        # Highly Available setup
#        cmd += " --internal_vip %s" % internal_vip
    if cassandra_user is not None:
        cmd += " -e CASSANDRA_USER=%s" % cassandra_user
        cmd += " -e CASSANDRA_PASSWORD=%s" % cassandra_password

    cmd += " -itd contrail-analytics-%s:%s" % (openstack_sku, contrail_version)
    return cmd


def frame_control_docker_cmd(host_string, contrail_version, openstack_sku):
    cfgm_host = get_control_host_string(env.roledefs['cfgm'][0])
    cfgm_ip = get_contrail_internal_vip() or hstr_to_ip(cfgm_host)
    control_host = get_control_host_string(host_string)
    tgt_ip = hstr_to_ip(control_host)
    collector_host_list=[]
    for entry in env.roledefs['collector']:
        collector_host_list.append(get_control_host_string(entry))
    control_host_list=[]
    for entry in env.roledefs['control']:
        control_host_list.append(get_control_host_string(entry))
    # Prefer local collector node
    if control_host in collector_host_list:
        collector_ip = tgt_ip
    else:
        # Select based on index
        hindex = control_host_list.index(control_host)
        hindex = hindex % len(env.roledefs['collector'])
        collector_host = get_control_host_string(env.roledefs['collector'][hindex])
        collector_ip = hstr_to_ip(collector_host)

    cmd = "docker run --net=host --restart=unless-stopped --name contrail-control"

    cmd += ' -e IPADDRESS=%s' % tgt_ip
    cmd += ' -e CONFIG_IP=%s' % cfgm_ip
# Need to see where collector_ip being used
#    cmd += ' --collector_ip %s' % collector_ip
    cmd += add_keystone_cmd_params()
    cmd += " -itd contrail-control-%s:%s" % (openstack_sku, contrail_version)
    return cmd


def frame_config_docker_cmd(host_string, contrail_version, openstack_sku):
    # start_config_cmd = "docker run --name contrail-config --net=host -e NEUTRON_PASSWORD=secret123
    # -e OS_TOKEN=%s  -e OS_PASSWORD=secret123 -e IPADDRESS=10.204.217.91 -e DISCOVERY_SERVER_PORT=9110
    # -itd contrail-config-liberty:3.0.2.0-35" % os_token

    nworkers = 1
    quantum_port = '9697'
    cfgm_host=get_control_host_string(host_string)
    tgt_ip = hstr_to_ip(cfgm_host)
    cmd = "docker run --net=host --restart=unless-stopped --name contrail-config"

    # Prefer local collector node
    cfgm_host_list = [get_control_host_string(entry)\
                     for entry in env.roledefs['cfgm']]
    collector_host_list = [get_control_host_string(entry)\
                          for entry in env.roledefs['collector']]
    if cfgm_host in collector_host_list:
        collector_ip = tgt_ip
    else:
        # Select based on index
        hindex = cfgm_host_list.index(cfgm_host)
        hindex = hindex % len(env.roledefs['collector'])
        collector_host = get_control_host_string(
                             env.roledefs['collector'][hindex])
        collector_ip = hstr_to_ip(collector_host)
    cassandra_ip_list = [hstr_to_ip(get_control_host_string(cassandra_host))\
                         for cassandra_host in env.roledefs['database']]
    orch = get_orchestrator()
    cassandra_user = get_cassandra_user()
    cassandra_password = get_cassandra_password()

    if get_mt_enable():
        cmd += " -e MULTI_TENANCY=True"

    contrail_internal_vip = get_contrail_internal_vip()
    if contrail_internal_vip:
        cmd += " -e CONTRAIL_INTERNAL_VIP=%s" % contrail_internal_vip

    cmd += " -e IPADDRESS=%s" % tgt_ip
    cmd += " -e COLLECTOR_SERVER=%s" % collector_ip
    cmd += ' -e CASSANDRA_SERVER_LIST="%s"' % ' '.join(cassandra_ip_list)
    cmd += ' -e ZOOKEEPER_SERVER_LIST="%s"' % ' '.join(cassandra_ip_list)
    cmd += " -e NEUTRON_PORT=%s" % quantum_port
    cmd += ' -e RABBITMQ_SERVER_LIST="%s"' % ' '.join(get_amqp_servers())
    cmd += " -e RABBITMQ_SERVER_PORT=%s" % get_amqp_port()

    # Affect is on ctrl-details for quantum_ip which will be localhost in case of haproxy
    #haproxy = get_haproxy()
    #if haproxy:
    #    cmd += " --haproxy %s" % haproxy
    #if orch == 'openstack':
    (_, openstack_admin_password) = get_authserver_credentials()
    cmd += add_keystone_cmd_params()
    # Pass keystone arguments in case for openstack orchestrator
    manage_neutron = get_manage_neutron()
#    if manage_neutron == 'no':
#        # Skip creating neutron service tenant/user/role etc in keystone.
#        cmd += ' --manage_neutron %s' % manage_neutron

# TODO need to handle vips
#    internal_vip = get_openstack_internal_vip()
#    contrail_internal_vip = get_contrail_internal_vip()
#    if internal_vip:
#        # Highly available openstack setup
#        cmd += ' --internal_vip %s' % (internal_vip)
#    if contrail_internal_vip:
#        # Highly available contrail setup
#        cmd += ' --contrail_internal_vip %s' % (contrail_internal_vip)
    if cassandra_user is not None:
        cmd += ' -e CASSANDRA_USER=%s' % (cassandra_user)
        cmd += ' -e CASSANDRA_PASSWORD=%s' % (cassandra_password)

    cmd += " -itd contrail-config-%s:%s" % (openstack_sku, contrail_version)
    return cmd

@task
@roles('all')
def set_hostname_permanent():
    """ In case of node provisioned through images, hostname would not have set permanent.
    """
    # Applicable on ubuntu
    hostname = run("hostname")
    if not exists("/etc/hostname"):
        sudo("echo {0} > /etc/hostname; sed -i '1i 127.0.0.1 {0}' /etc/hosts; service hostname restart".format(hostname))


def frame_database_docker_cmd(host_string, contrail_version, openstack_sku):
    database_host = host_string
    cfgm_host = get_control_host_string(env.roledefs['cfgm'][0])
    cfgm_ip = get_contrail_internal_vip() or hstr_to_ip(cfgm_host)
    database_host_list = [get_control_host_string(entry)\
                          for entry in env.roledefs['database']]
    database_ip_list = [hstr_to_ip(db_host) for db_host in database_host_list]
    database_host=get_control_host_string(host_string)
    database_host_password=get_env_passwords(host_string)
    tgt_ip = hstr_to_ip(database_host)
    #derive kafka broker id from the list of servers specified
    broker_id = sorted(database_ip_list).index(tgt_ip)
    cassandra_user = get_cassandra_user()
    cassandra_password = get_cassandra_password()
    cmd = "docker run --net=host --restart=unless-stopped --name contrail-database"
    cmd += " -e IPADDRESS=%s" % tgt_ip
    cmd += " -e CFGM_IP=%s" % cfgm_ip
    database_dir = get_database_dir()
    if database_dir is not None:
        cmd += " -e DATA_DIR=%s" % database_dir
    analytics_data_dir = get_analytics_data_dir()
    if analytics_data_dir is not None:
        cmd += " -e ANALYTICS_DATA_DIR=%s" % analytics_data_dir
    ssd_data_dir = get_ssd_data_dir()
    if ssd_data_dir is not None:
        cmd += " -e SSD_DATA_DIR=%s" % ssd_data_dir
    if (len(env.roledefs['database'])>2):
        cmd += " -e SEED_LIST=\"%s\"" % ','.join(database_ip_list[:2])
    else:
        cmd += " -e SEED_LIST=\"%s\"" % (hstr_to_ip(get_control_host_string(
                                       env.roledefs['database'][0])))
    cmd += " -e ZOOKEEPER_IP_LIST=\"%s\"" % ' '.join(database_ip_list)
    cmd += " -e DATABASE_INDEX=%d" % (database_host_list.index(database_host) + 1)
    minimum_diskGB = get_minimum_diskGB()
    if minimum_diskGB is not None:
        cmd += " -e MINIMUM_DISKGB=%s" % minimum_diskGB
    cmd += " -e KAFKA_BROKER_ID=%d" % broker_id
    if cassandra_user is not None:
        cmd += " -e CASSANDRA_USER=%s" % cassandra_user
        cmd += " -e CASSANDRA_PASSWORD=%s" % cassandra_password

    cmd += " -itd contrail-database-%s:%s" % (openstack_sku, contrail_version)
    return cmd


def frame_lb_docker_cmd(contrail_version, openstack_sku):
    config_node_list = ",".join([re.sub(r'^\w+@','',i) for i in env.roledefs['cfgm']])
    my_ip = env.host_string.split('@')[1]
    cmd = "docker run --net=host --restart=unless-stopped --name contrail-lb --privileged --cap-add=NET_ADMIN "
    cmd += " -e IPADDRESS=%s" % my_ip
    cmd += " -e NEUTRON_SERVER_LIST=%s " % config_node_list
    cmd += " -e CONTRAIL_API_SERVER_LIST=%s " % config_node_list
    cmd += " -e DISCOVERY_SERVER_LIST=%s " % config_node_list
    contrail_internal_vip = get_contrail_internal_vip()
    if contrail_internal_vip:
        cmd += " -e HA_ENABLED=yes -e INTERNAL_VIP=%s" % contrail_internal_vip
        cmd += " -e HA_NODE_IP_LIST=%s " % config_node_list
        node_index = config_node_list.split(',').index(my_ip) + 1
        cmd += " -e NODE_INDEX=%s" % node_index
        cmd += ' -e RABBITMQ_SERVER_LIST="%s"' % ','.join(get_amqp_servers())
    cmd += " -itd contrail-loadbalancer-%s:%s" % (openstack_sku, contrail_version)
    return cmd

@task
def start_container(component, contrail_version, openstack_sku):
    if component == 'database':
        run_command = frame_database_docker_cmd(env.host_string, contrail_version, openstack_sku)
    elif component == 'config':
        run_command = frame_config_docker_cmd(env.host_string, contrail_version, openstack_sku)
    elif component == 'control':
        run_command = frame_control_docker_cmd(env.host_string, contrail_version, openstack_sku)
    elif component == 'analytics':
        run_command = frame_analytics_docker_cmd(env.host_string, contrail_version, openstack_sku)
    elif component == 'lb':
        run_command = frame_lb_docker_cmd(contrail_version, openstack_sku)

    sudo(run_command)

@task
@roles('cfgm')
def initialize(package_path):
    package_name = os.path.basename(package_path)
    if not exists("/opt/contrail/contrail_packages/setup.sh"):
        put(package_path, "/tmp/")
        cmd = "dpkg -i /tmp/%s; " % package_name
        cmd += "cd /opt/contrail/contrail_packages; bash setup.sh"
        sudo(cmd)

@task
@roles('cfgm')
def install_rabbit():
    sudo("DEBIAN_FRONTEND=noninteractive apt-get -y --force-yes --allow-unauthenticated install rabbitmq-server")

@task
def prepare_node(contrail_install_package_url, *tgzs, **kwargs):
    temp_dir = tempfile.mkdtemp()
    package_file_path = os.path.join(temp_dir, os.path.basename(contrail_install_package_url))
    local("wget -q -O %s %s" % (package_file_path, contrail_install_package_url))
    execute("install_pkg_all", package_file_path)
    execute("create_installer_repo")
    execute("create_install_repo", *tgzs, **kwargs)
    local("rm -fr %s" % temp_dir)
    execute(set_hostname_permanent)


@task
@roles('build')
def install_on_host(*tgzs, **kwargs):
    reboot = kwargs.get('reboot', 'True')
    execute('pre_check')
    execute(install_rabbit)
    execute(create_install_repo_dpdk)
    execute('install_orchestrator')
    execute(install_docker)
    if 'vcenter_compute' in env.roledefs:
        execute(install_vcenter_compute)
    execute(install_vrouter)
    if getattr(env, 'interface_rename', True):
        print "Installing interface Rename package and rebooting the system."
        execute(install_interface_name, reboot)
        #Clear the connections cache
        connections.clear()
    execute('reboot_on_kernel_update', reboot)

@task
@roles('build')
def ha_setup(docker_images, contrail_version, openstack_sku):
    """Setup ha - contrail-fabric-utils ha.py does setup lb for contrail in the machine
    which is not required as that is going to be in container.
    """
    execute('pre_check')
    contrail_internal_vip = get_contrail_internal_vip()
    if contrail_internal_vip:
        execute(load_docker_image, docker_images["loadbalancer"], roles=["cfgm"])
        execute(start_container, "lb", contrail_version, openstack_sku, roles=["cfgm"])

    if get_openstack_internal_vip():
        print "Multi Openstack setup, provisioning openstack HA."
        execute('setup_galera_cluster')
        execute('fix_wsrep_cluster_address')
        execute('setup_cmon_schema')
        execute('fix_restart_xinetd_conf')
        execute('fixup_restart_haproxy_in_openstack')
        execute('setup_glance_images_loc')
        execute('fix_memcache_conf')
        execute('tune_tcp')
        execute('fix_cmon_param_and_add_keys_to_compute')
        execute('create_and_copy_service_token')

@task
@roles('build')
def setup(docker_images, contrail_version, openstack_sku, reboot='True'):
    with settings(warn_only=True):
        execute('setup_common')
        execute(ha_setup, docker_images, contrail_version, openstack_sku)
        execute('setup_rabbitmq_cluster')
        execute('increase_limits')
        execute(load_docker_image, docker_images["database"], roles=["cfgm"])
        execute(start_container, 'database', contrail_version, openstack_sku, roles=["database"])
    #    execute('verify_database') - verify_service will not work on container, would need to have a replacement
    #    execute('fixup_mongodb_conf') - This need to be done on container, skipping as of now
    #    execute('setup_mongodb_ceilometer_cluster') - this is required but skipping as of now (may be ceilometer is not setup at this stage
        execute('setup_orchestrator')
        os_token = local("cat /etc/contrail/service.token", capture=True)
        execute(load_docker_image, docker_images["config"], roles=["cfgm"])
        execute(start_container, "config", contrail_version, openstack_sku, roles=["cfgm"])
        execute(load_docker_image, docker_images["control"], roles=["cfgm"])
        execute(start_container, "control", contrail_version, openstack_sku, roles=["cfgm"])
        execute(load_docker_image, docker_images["analytics"], roles=["cfgm"])
        execute(start_container, "analytics", contrail_version, openstack_sku, roles=["cfgm"])
    #    execute('verify_cfgm') - Nothing as of now, will need to check
    #    execute('verify_control') - Nothing as of now, will need to check
    #    execute('verify_collector') - Nothing as of now, will need to check
    #    execute('verify_webui') - Nothing as of now, will check
        execute('setup_vrouter')
        execute('prov_config') # - This should be done in config node (while starting container) or on orchestrator
        execute('prov_database')  # This should be done in config container or or on orchestrator
        execute('prov_analytics')
        execute('prov_control_bgp')
        execute('prov_external_bgp')
        execute('prov_metadata_services')
        execute('prov_encap_type') # till this done
        execute('setup_remote_syslog')
        execute('add_tsn', restart=False)
        execute('add_tor_agent', restart=False)
        execute('increase_vrouter_limit')
        execute('setup_vm_coremask')
        if get_openstack_internal_vip():
            execute('setup_cluster_monitors')
        if reboot == 'True':
            print "Rebooting the compute nodes after setup all."
            execute('compute_reboot')
            #Clear the connections cache
            connections.clear()
            execute('verify_compute')
        execute('setup_nova_aggregate')
#end setup_all


def copy_url(url, destination):
    if re.match(r"^http[s]?://", url):
        with settings(warn_only=True):
            a = local("wget -q -O %s %s" % (destination, url))
            if a.suceeded:
                return True
            else:
                return False
    elif re.match(r"^/", url):
        if os.path.isfile(url):
            if not os.path.samefile(url, destination):
                with settings(warn_only=True):
                    local("cp -f %s %s" % (url, destination))
                return True


def update_env_passwords(host, password=None):
    node_hoststring = (host
                       if re.match(r'\w+@[\d\.]+:\d+', host)
                       else host + ':22')
    if password:
        env.passwords.update({node_hoststring: password})


def main(argv=sys.argv[1:]):

    ap = argparse.ArgumentParser(description='Contrail orchestrator')
    sp = ap.add_subparsers(dest='resource')
    p_prov = sp.add_parser('provision', help='manage provisioning')
    p_prov.add_argument('testbed', type=str,
                    help="Testbed url/path, it can be local"
                         "path or web url")
    p_prov.add_argument('-c', '--contrail-version', type=str, required=True,
                        help="Contrail version")
    p_prov.add_argument('-o', '--openstack-sku', type=str, required=True,
                        choices=["juno", "kilo", "liberty","mitaka"],
                        help="Openstack release")
    p_prov.add_argument('-n', '--no-host-setup', action='store_true', default=False,
                        help="Contrail version")
    p_prov.add_argument('-u', '--contrail-package-url', type=str, required=True,
                        help="Contrail install package http[s] url")

    args = ap.parse_args()
    testbed_path = "/opt/contrail/utils/fabfile/testbeds/testbed.py"
    copy_url(args.testbed, testbed_path)
    sys.path.append(os.path.dirname(testbed_path))
    import testbed
    docs, callables, default = load_fabfile('/opt/contrail/utils/fabfile')
    state.commands.update(callables)

    # hoststrings in env.passwords should be in form of user@host:port
    # Fixing any invalid hoststring entries
    passwords = env.passwords.copy()
    for host, password in passwords.iteritems():
        update_env_passwords(host, password)

    contrail_service_containers = ['config','control','analytics','database','loadbalancer']
    container_base_image_url = testbed.contrail_docker_image_base_url
    docker_images = {
        component: "%s/contrail-%s-%s-%s.tar.gz" % (container_base_image_url,
                                                    component,
                                                    args.openstack_sku,
                                                    args.contrail_version)
            for component in contrail_service_containers
        }

    if not args.no_host_setup:
        execute(prepare_node, args.contrail_package_url)
        execute(install_on_host, reboot=False)
    execute(setup, docker_images, args.contrail_version, args.openstack_sku)
    return True

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
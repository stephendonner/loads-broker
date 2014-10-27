"""AWS Higher Level Abstractions

This module contains higher-level AWS abstractions to make working with
AWS instances and collections of instances easier and less error-prone.

:ref:`EC2instance` is responsible for maintaining information about
itself and updating its state when asked to. The executer passed in
must be capable of running functions that may block, ie a Greenlet or
ThreadPool executor.

:ref:`EC2Collection` is a group of instances for a given allocation
request. Collections should be passed back to the Pool when their use
is no longer required.

An EC2 Pool is responsible for allocating and dispersing
:ref:`EC2Instance`s and terminating idle instances.

The :ref:`EC2Pool` is responsible for tracking EC2 instances across
regions, allocating them for use by the broker, and terminating
excessively idle instances. It also can rebuild maps of existing
instances by querying AWS for appropriate instance types.

"""
import concurrent.futures
import time
import os
from collections import defaultdict
from datetime import datetime, timedelta
from shlex import quote
from string import Template
from io import StringIO
from random import randint

from boto.ec2 import connect_to_region
from tornado import gen
from tornado.concurrent import Future
from tornado.httpclient import AsyncHTTPClient
import tornado.ioloop
import paramiko.client as sshclient

from loadsbroker.dockerctrl import DockerDaemon
from loadsbroker.exceptions import LoadsException, TimeoutException
from loadsbroker.ssh import makedirs
from loadsbroker import logger


AWS_REGIONS = (
    "ap-northeast-1", "ap-southeast-1", "ap-southeast-2",
    "eu-west-1",
    "sa-east-1",
    "us-east-1", "us-west-1", "us-west-2"
)


# Initial blank list of AMI ID's that will map a region to a dict keyed by
# virtualization type of the appropriate AMI to use
AWS_AMI_IDS = {k: {} for k in AWS_REGIONS}


# The Heka configuration file template. Heka containers on each instance
# forward messages to a central Heka server via TcpOutput.
HEKA_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "hekad.src.toml")

with open(HEKA_CONFIG_PATH, "r") as f:
    HEKA_CONFIG_TEMPLATE = Template(f.read())


# Default ping request options.
_PING_DEFAULTS = {
    "method": "HEAD",
    "headers": {"Connection": "close"},
    "follow_redirects": False
}


def populate_ami_ids(aws_access_key_id=None, aws_secret_access_key=None,
                     port=None, owner_id="595879546273"):
    """Populate all the AMI ID's with the latest CoreOS stable info.

    This is a longer blocking operation and should be done on startup.
    """
    # see https://github.com/boto/boto/issues/2617
    if port is not None:
        is_secure = port == 443
    else:
        is_secure = True

    # Spin up a temp thread pool to make this faster
    errors = []

    def get_amis(region):
        try:
            conn = connect_to_region(
                region,
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key,
                port=port, is_secure=is_secure)

            filters = {}
            if owner_id is not None:
                filters["owner-id"] = owner_id

            images = conn.get_all_images(filters=filters)

            # The last two highest sorted are the pvm and hvm instance id's
            images = sorted([x for x in images if "stable" in x.name],
                            key=lambda x: x.name)[-2:]

            AWS_AMI_IDS[region] = {x.virtualization_type: x for x in images}
        except Exception as exc:
            errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(len(AWS_REGIONS)) as pool:
        # Execute all regions in parallel.
        pool.map(get_amis, AWS_REGIONS)

    if len(errors) > 0:
        raise errors[0]


def get_ami(region, instance_type):
    """Returns the appropriate AMI to use for a given region + instance type

    HVM is always used except for instance types which cannot use it. Based
    on matrix here:

    http://aws.amazon.com/amazon-linux-ami/instance-type-matrix/

    .. note::

        :ref:`populate_ami_ids` must be called first to populate the available
        AMI's.

    """
    instances = AWS_AMI_IDS[region]

    inst_type = "hvm"
    if instance_type[:2] in ["m1", "m2", "c1", "t1"]:
        inst_type = "paravirtual"

    if inst_type not in instances:
        raise KeyError("Could not find instance type %r in %s" % (
            inst_type,
            list(instances.keys())))

    return instances[inst_type].id


def available_instance(instance):
    """Returns True if an instance is usable for allocation.

    Instances are only usable if they're running, or have been
    "pending" for less than 2 minutes. Instances pending more than
    2 minutes are likely perpetually stalled and will be reaped.

    :type instance: :ref:`instance.Instance`
    :returns: Whether the instance should be used for allocation.
    :rtype: bool

    """
    if instance.state == "running":
        return True

    if instance.state == "pending":
        oldest = datetime.today() - timedelta(minutes=2)
        launched = datetime.strptime(instance.launch_time,
                                     '%Y-%m-%dT%H:%M:%S.%fZ')
        if oldest < launched:
            return True

    return False


class EC2Instance:
    """Creates an instance.

    :type instance: :ref:`instance.Instance`
    :type conn: :ref:`connection.EC2Connection`
    :type executer: :ref:`concurrent.futures.Executor`

    """
    def __init__(self, instance, conn, executer, io_loop=None,
                 ssh_keyfile=None):
        self.state = instance.state
        self.type = instance.instance_type
        self._retries = 5
        self._instance = instance
        self._executer = executer
        self._docker = None
        self._ssh_keyfile = ssh_keyfile
        self._loop = io_loop or tornado.ioloop.IOLoop.instance()
        self._ping_client = AsyncHTTPClient(io_loop=self._loop,
                                            defaults=_PING_DEFAULTS)

    def connect(self):
        """Opens an SSH connection to this instance."""
        client = sshclient.SSHClient()
        client.set_missing_host_key_policy(sshclient.AutoAddPolicy())
        client.connect(self._instance.ip_address, username="core",
                       key_filename=self._ssh_keyfile)
        return client

    @gen.coroutine
    def update_state(self, retries=None):
        """Updates the state of this instance."""
        max_tries = retries or self._retries
        tries = 0
        while tries < max_tries:
            try:
                self.state = yield self._executer.submit(self._instance.update)
                break
            except Exception:
                logger.debug(
                    "Error loading state for instsance %s, try %s of %s",
                    self._instance.id, tries, max_tries)
                tries += 1
                yield gen.Task(self._loop.add_timeout, time.time() + 1)

    @gen.coroutine
    def wait_for_state(self, state, interval=5, timeout=600):
        """Continually updates the state until the target state is reached
        or the timeout is hit.

        Defaults to a time-out of 10 minutes with 5 seconds between each
        check.

        :raises:
            :exc: `TimeoutException` if timeout is exceeded without the
                state change occurring.

        """
        if self.state == state:
            return

        end = time.time() + timeout
        while self.state != state:
            if time.time() > end:
                raise TimeoutException()

            try:
                yield self.update_state()
            except:
                # This can fail too early on occasion
                logger.error("Instance not found yet.", exc_info=True)

            if self.state != state:
                yield gen.Task(self._loop.add_timeout, time.time() + interval)

    @gen.coroutine
    def wait_for_docker(self, interval=5, timeout=600):
        """Waits till docker is available on the host."""
        end = time.time() + timeout

        # First, wait till we're running
        yield self.wait_for_state("running")

        # Ensure we have a docker daemon for ourself
        #
        # XXX use the fake local docker in case
        # the instance is faked
        if self._instance.ip_address is None:
            docker_host = 'tcp://0.0.0.0:7890'
        else:
            docker_host = "tcp://%s:2375" % self._instance.ip_address

        if not self._docker:
            self._docker = DockerDaemon(host=docker_host)

        # Attempt to fetch until it works
        success = False
        while not success:
            try:
                yield self._executer.submit(self._docker.get_containers)
                success = True
            except Exception:
                # Wait 5 seconds to try again
                yield gen.Task(self._loop.add_timeout, time.time() + interval)

                if time.time() > end:
                    raise TimeoutException()

    @gen.coroutine
    def start_cadvisor(self, database_name, options):
        """Launches a cAdvisor container on the instance."""

        volumes = {
            '/': {'bind': '/rootfs', 'ro': True},
            '/var/run': {'bind': '/var/run', 'ro': False},
            '/sys': {'bind': '/sys', 'ro': True},
            '/var/lib/docker': {'bind': '/var/lib/docker', 'ro': True}
        }

        logger.debug("cAdvisor: Writing stats to %s" % database_name)

        yield self.run_container("google/cadvisor:latest", None, " ".join([
            "-storage_driver=influxdb",
            "-log_dir=/",
            "-storage_driver_db=%s" % quote(database_name),
            "-storage_driver_host=%s:%d" % (quote(options.host),
                                            options.port),
            "-storage_driver_user=%s" % quote(options.user),
            "-storage_driver_password=%s" % quote(options.password),
            "-storage_driver_secure=%d" % options.secure
        ]), volumes=volumes, ports={8080: 8080})

        health_url = "http://%s:8080/healthz" % self._instance.ip_address
        yield self._ping(health_url)

    @gen.coroutine
    def stop_cadvisor(self):
        """Stops all cAdvisor containers on the instance."""
        yield self.stop_container("google/cadvisor:latest", 5)

    def _upload_heka_config(self, config_file):
        client = self.connect()
        try:
            sftp = client.open_sftp()
            try:
                # Create the Heka data directory on the instance.
                makedirs(sftp, "/home/core/heka")

                # Copy the Heka configuration file.
                sftp.putfo(config_file, "/home/core/heka/config.toml")
            finally:
                sftp.close()
        finally:
            client.close()

    @gen.coroutine
    def start_heka(self, config_file):
        """Launches a Heka container on the instance."""

        yield self._executer.submit(self._upload_heka_config, config_file)

        volumes = {'/home/core/heka': {'bind': '/heka', 'ro': False}}
        ports = {(8125, "udp"): 8125, 4352: 4352}

        yield self.run_container("kitcambridge/heka:dev", None,
                                 "-config=/heka/config.toml",
                                 volumes=volumes, ports=ports)

        health_url = "http://%s:4352/" % self._instance.ip_address
        yield self._ping(health_url)

    @gen.coroutine
    def stop_heka(self):
        """Stops all Heka containers on the instance."""
        yield self.stop_container("kitcambridge/heka:dev", 15)

    @gen.coroutine
    def _ping(self, url, attempts=5, delay=0.5, max_jitter=0.2,
              max_delay=15, **options):
        attempt = 1
        while attempt < attempts:
            try:
                yield self._ping_client.fetch(url, **options)
                return True
            except ConnectionError:
                jitter = randint(0, max_jitter * 100) / 100
                yield gen.Task(self._loop.add_timeout,
                               time.time() + delay + jitter)
                attempt += 1
                delay = min(delay * 2, max_delay)
        raise

    @gen.coroutine
    def is_running(self, container_name):
        """Checks running instances to see if the provided
        container_name is running on the instance."""
        all_containers = yield self._executer.submit(
            self._docker.get_containers)

        for cid, container in all_containers.items():
            if container_name in container["Image"]:
                return True

        return False

    def _import_container(self, container_url):
        client = self.connect()
        try:
            output = self._docker.import_container(client, container_url)
        finally:
            client.close()
        return output

    @gen.coroutine
    def load_container(self, container_name, container_url):
        """Loads's a container of the provided name to the instance."""
        has_container = yield self._executer.submit(
            self._docker.has_image, container_name)
        if has_container:
            return

        if container_url:
            output = yield self._executer.submit(self._import_container,
                                                 container_url)
        else:
            output = yield self._executer.submit(self._docker.pull_container,
                                                 container_name)

        has_container = yield self._executer.submit(
            self._docker.has_image, container_name)
        if not has_container:
            raise LoadsException("Unable to load container: %s", output)

    @gen.coroutine
    def run_container(self, container_name, env, command_args,
                      volumes={}, ports={}):
        """Run a container of the provided name with the env/command
        args supplied."""
        yield self._executer.submit(self._docker.run_container,
                                    container_name, env, command_args,
                                    volumes, ports)

    @gen.coroutine
    def kill_container(self, container_name):
        """Kill the container with the provided name."""
        yield self._executer.submit(self._docker.kill_container,
                                    container_name)

    @gen.coroutine
    def stop_container(self, container_name, timeout=15):
        """Gracefully stops the container with the provided name and
        timeout."""
        yield self._executer.submit(self._docker.stop_container,
                                    container_name, timeout)


class EC2Collection:
    """Create a collection to manage a set of instances.

    :type instances: list of :ref:`instance.Instance`

    """
    def __init__(self, run_id, uuid, conn, instances, io_loop=None,
                 ssh_keyfile=None, heka_options=None, influx_options=None):
        self.run_id = run_id
        self.uuid = uuid
        self.started = False
        self.finished = False
        self._container = None
        self._env_data = None
        self._command_args = None
        self._executer = concurrent.futures.ThreadPoolExecutor(len(instances))
        self._loop = io_loop or tornado.ioloop.IOLoop.instance()
        self._ssh_keyfile = ssh_keyfile
        self._heka_options = heka_options
        self._influx_options = influx_options

        self._instances = []
        for inst in instances:
            ec2inst = EC2Instance(inst, conn, self._executer, self._loop,
                                  ssh_keyfile)
            self._instances.append(ec2inst)

    @gen.coroutine
    def wait_for_docker(self):
        """Wait till all the instances are ready for docker commands."""
        yield [inst.wait_for_docker() for inst in self._instances]

        # Next, wait till additional containers are ready on the hosts
        logger.debug("Loading Heka and cAdvisor...")
        yield [self.load_container("kitcambridge/heka:dev"),
               self.load_container("google/cadvisor:latest")]

    @gen.coroutine
    def start_hekas(self):
        """Launches Heka containers on all instances."""

        if not self._heka_options:
            logger.debug("Heka not configured")
            return

        remote_host = self._heka_options.host
        if ":" in remote_host or "%" in remote_host:
            remote_host = "[" + remote_host + "]"

        config_file = HEKA_CONFIG_TEMPLATE.substitute(
            remote_host=remote_host,
            remote_port=self._heka_options.port,
            remote_secure=self._heka_options.secure and "true" or "false")

        @gen.coroutine
        def start_heka(inst):
            with StringIO(config_file) as fl:
                yield inst.start_heka(fl)

        logger.debug("Launching Heka...")
        yield [start_heka(inst) for inst in self._instances]

    @gen.coroutine
    def stop_hekas(self):
        """Stops all running Heka containers on each instance."""
        logger.debug("Stopping Heka...")
        yield [inst.stop_heka() for inst in self._instances]

    @gen.coroutine
    def start_cadvisors(self):
        """Launches cAdvisor containers on all instances."""

        if not self._influx_options:
            logger.debug("InfluxDB not configured; skipping cAdvisor")
            return

        logger.debug("Launching cAdvisor...")

        database_name = "%s-cadvisor" % self.run_id
        yield [inst.start_cadvisor(database_name, self._influx_options)
               for inst in self._instances]

    @gen.coroutine
    def stop_cadvisors(self):
        """Stops all running cAdvisor containers on each instance."""
        logger.debug("Stopping cAdvisor...")
        yield [inst.stop_cadvisor() for inst in self._instances]

    def set_container(self, container_name, container_url=None, env_data="",
                      command_args=""):
        self._container = container_name
        self._container_url = container_url
        self._env_data = [x.strip() for x in env_data.splitlines()]
        self._command_args = command_args

    @gen.coroutine
    def load_container(self, container_name=None, container_url=None):
        if not container_name:
            container_name = self._container
            container_url = self._container_url

        if not container_name:
            raise LoadsException("No container name to use for EC2Collection: "
                                 "RunId: %s, Uuid: %s" % (self.run_id,
                                                          self.uuid))

        yield [inst.load_container(container_name, container_url)
               for inst in self._instances]

    @gen.coroutine
    def run_containers(self, container_name, env, command_args, volumes={},
                       ports={}):
        yield [inst.run_container(container_name, env, command_args,
               volumes, ports) for inst in self._instances]

    @gen.coroutine
    def is_running(self):
        """Are any of the instances still running the set container.

        :returns: Indicator if any of the instances in the collection
                  are still running.
        :rtype: bool

        """
        running = yield [inst.is_running(self._container)
                         for inst in self._instances]
        return any(running)

    @gen.coroutine
    def start(self):
        """Start up a run"""
        if self.started:
            return

        self.started = True

        # Launch Heka and cAdvisor.
        yield [self.start_hekas(), self.start_cadvisors()]

        # Launch the test agent.
        yield self.run_containers(self._container, self._env_data,
                                  self._command_args)

    @gen.coroutine
    def shutdown(self):
        if self.finished:
            return
        self.finished = True

        # Stop the test agent.
        yield [inst.kill_container(self._container)
               for inst in self._instances]

        # Stop Heka and cAdvisor.
        yield [self.stop_hekas(), self.stop_cadvisors()]


class EC2Pool:
    """Initialize a pool for instance allocation and recycling.

    All instances allocated using this pool will be tagged as follows:

    Name
        loads-BROKER_ID
    Broker
        BROKER_ID
    Run (if this instance is currently associate with a Run)
        RUN_ID

    .. warning::

        This instance is **NOT SAFE FOR CONCURRENT USE BY THREADS**.

    """
    def __init__(self, broker_id, access_key=None, secret_key=None,
                 key_pair="loads", security="loads", max_idle=600,
                 user_data=None, io_loop=None, port=None,
                 owner_id="595879546273", use_filters=True,
                 ssh_keyfile=None, heka_options=None, influx_options=None):
        self.owner_id = owner_id
        self.use_filters = use_filters
        self.broker_id = broker_id
        self.access_key = access_key
        self.secret_key = secret_key
        self.max_idle = max_idle
        self.key_pair = key_pair
        self.ssh_keyfile = ssh_keyfile
        self.heka_options = heka_options
        self.influx_options = influx_options
        self.security = security
        self.user_data = user_data
        self._instances = defaultdict(list)
        self._tag_filters = {"tag:Name": "loads-%s" % self.broker_id,
                             "tag:Project": "loads"}
        self._conns = {}
        self._recovered = {}
        self._executor = concurrent.futures.ThreadPoolExecutor(15)
        self._loop = io_loop or tornado.ioloop.IOLoop.instance()
        self.port = port
        # see https://github.com/boto/boto/issues/2617
        if port is not None:
            self.is_secure = port == 443
        else:
            self.is_secure = True

        # Asynchronously initialize ourself when the pool runs
        self._loop.add_future(
            self.initialize(),
            lambda x: logger.debug("Finished initializing. %s", x.result())
        )

        self.ready = Future()

    def shutdown(self):
        """Make sure we shutdown the executor.
        """
        self._executor.shutdown()

    def initialize(self):
        """Fully initialize the AWS pool and dependencies, recover existing
        instances, etc.

        :returns: A future that will require the loop running to retrieve.

        """
        logger.debug("Pulling CoreOS AMI info...")
        populate_ami_ids(self.access_key, self.secret_key, port=self.port,
                         owner_id=self.owner_id)
        return self._recover()

    def _initialized(self, future):
        # Run the result to ensure we raise an exception if any occurred
        future.result()
        logger.debug("Finished initializing.")
        self.ready.set_result(True)

    @gen.coroutine
    def _region_conn(self, region=None):
        if region in self._conns:
            return self._conns[region]

        # Setup a connection
        logger.debug("Requesting connection for region: %s", region)
        conn = yield self._executor.submit(
            connect_to_region, region,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            port=self.port, is_secure=self.is_secure)

        self._conns[region] = conn
        logger.debug("Returning connection for region: %s", region)
        return conn

    @gen.coroutine
    def _recover_region(self, region):
        """Recover all the instances in a region"""
        conn = yield self._region_conn(region)
        logger.debug("Requesting instances for %s", region)

        if self.use_filters:
            filters = self._tag_filters
        else:
            filters = {}

        instances = yield self._executor.submit(
            conn.get_only_instances,
            filters=filters)

        logger.debug("Finished requesting instances for %s", region)
        return instances

    @gen.coroutine
    def _recover(self):
        """Recover allocated instances from EC2."""
        recovered_instances = defaultdict(list)

        # Recover every region at once
        instancelist = yield [self._recover_region(x) for x in AWS_REGIONS]

        logger.debug("Found %s instances to recover.",
                     sum(map(len, instancelist)))

        for instances in instancelist:
            for instance in instances:
                tags = instance.tags

                # If this has been 'pending' too long, we put it in the main
                # instance pool for later reaping
                if not available_instance(instance):
                    self._instances[instance.region.name].append(instance)
                    continue

                if tags.get("RunId") and tags.get("Uuid"):
                    # Put allocated instances into a recovery pool separate
                    # from unallocated
                    inst_key = (tags["RunId"], tags["Uuid"])
                    recovered_instances[inst_key].append(instance)
                else:
                    self._instances[instance.region.name].append(instance)
        self._recovered = recovered_instances

    def _locate_recovered_instances(self, run_id, uuid):
        """Locates and removes existing allocated instances if any"""
        key = run_id, uuid

        if key not in self._recovered:
            # XXX do we want to raise here?
            return []

        instances = self._recovered[key]
        del self._recovered[key]
        return instances

    def _locate_existing_instances(self, count, inst_type, region):
        """Locates and removes existing available instances if any."""
        region_instances = self._instances[region]
        instances = []
        remaining = []

        for inst in region_instances:
            if available_instance(inst) and inst_type == inst.instance_type:
                    instances.append(inst)
            else:
                remaining.append(inst)

            if len(instances) > count:
                break

        # Determine how many were removed, and reconstruct the unallocated
        # instance list with the instances not used
        removed = len(instances) + len(remaining)
        self._instances[region] = region_instances[removed:] + remaining
        return instances

    @gen.coroutine
    def _allocate_instances(self, conn, count, inst_type, region):
        """Allocate a set of new instances and return them."""
        ami_id = get_ami(region, inst_type)
        reservations = yield self._executor.submit(
            conn.run_instances,
            ami_id, min_count=count, max_count=count,
            key_name=self.key_pair, security_groups=[self.security],
            user_data=self.user_data, instance_type=inst_type)

        return reservations.instances

    @gen.coroutine
    def request_instances(self, run_id, uuid, count=1, inst_type="t1.micro",
                          region="us-west-2"):
        """Allocate a collection of instances.

        :param run_id: Run ID for these instances
        :param uuid: UUID to use for this collection
        :param count: How many instances to allocate
        :param type: EC2 Instance type the instances should be
        :param region: EC2 region to allocate the instances in
        :returns: Collection of allocated instances
        :rtype: :ref:`EC2Collection`

        """
        if region not in AWS_REGIONS:
            raise LoadsException("Unknown region: %s" % region)

        # First attempt to recover instances for this run/uuid
        instances = self._locate_recovered_instances(run_id, uuid)
        remaining_count = count - len(instances)

        # Add any more remaining that should be used
        instances.extend(
            self._locate_existing_instances(remaining_count, inst_type, region)
        )

        conn = yield self._region_conn(region)

        # Determine if we should allocate more instances
        num = count - len(instances)
        if num > 0:
            new_instances = yield self._allocate_instances(
                conn, num, inst_type, region)
            logger.debug("Allocated instances: %s", new_instances)
            instances.extend(new_instances)

        # Tag all the instances
        if self.use_filters:
            yield self._executor.submit(
                conn.create_tags,
                [x.id for x in instances],
                {
                    "Name": "loads-%s" % self.broker_id,
                    "Project": "loads",
                    "RunId": run_id,
                    "Uuid": uuid
                }
            )
        return EC2Collection(run_id, uuid, conn, instances,
                             self._loop, self.ssh_keyfile,
                             self.heka_options, self.influx_options)

    @gen.coroutine
    def release_instances(self, collection):
        """Return a collection of instances to the pool.

        :param collection: Collection to return
        :type collection: :ref:`EC2Collection`

        """
        instance = collection._instances[0]._instance
        region = instance.region.name
        instances = [x._instance for x in collection._instances]

        # De-tag the Run data on these instances
        conn = yield self._region_conn(region)

        if self.use_filters:
            yield self._executor.submit(
                conn.create_tags,
                [x.id for x in instances],
                {"RunId": "", "Uuid": ""})

        self._instances[region].extend(instances)

    @gen.coroutine
    def reap_instances(self):
        """Immediately reap all instances."""
        # Remove all the instances before yielding actions
        all_instances = self._instances
        self._instances = defaultdict(list)

        for region, instances in all_instances.items():
            conn = yield self._region_conn(region)

            # submit these instances for termination
            yield self._executor.submit(
                conn.terminate_instances,
                [x.id for x in instances])

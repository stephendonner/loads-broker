"""Broker Orchestration

The Broker is responsible for:

* Coordinating runs
* Ensuring run transitions
* Providing a rudimentary public API for use by the CLI/Web clients

"""
import os
from functools import partial
from uuid import uuid4

from tornado import gen

from loadsbroker import logger, aws
from loadsbroker.api import _DEFAULTS
from loadsbroker.db import Database, Run, RUNNING, COMPLETED


class Broker:
    def __init__(self, io_loop, sqluri, ssh_key, ssh_username, aws_port=None,
                 aws_owner_id="595879546273", aws_use_filters=True):
        self.loop = io_loop
        user_data = _DEFAULTS["user_data"]
        if user_data is not None and os.path.exists(user_data):
            with open(user_data) as f:
                user_data = f.read()

        self.pool = aws.EC2Pool("1234", user_data=user_data,
                                io_loop=self.loop, port=aws_port,
                                owner_id=aws_owner_id,
                                use_filters=aws_use_filters)

        self.db = Database(sqluri, echo=True)
        self.sqluri = sqluri
        self.ssh_key = ssh_key
        self.ssh_username = ssh_username

        # Run managers keyed by uuid
        self._runs = {}

    def shutdown(self):
        self.pool.shutdown()

    def get_runs(self):
        # XXX filters, batching
        runs = self.db.session().query(Run).all()
        return [run.json() for run in runs]

    @gen.coroutine
    def _test(self, run, session, collection):
        # let's create in the Database the collection
        db_collection = Collection(
            name='welp',
            uuid=collection.uuid,
            instance_count=len(collection.instances))

        session.add(db_collection)
        run.status = RUNNING
        session.commit()

        # Wait for all the instances to come up
        yield collection.wait_for_docker()
        logger.debug("Finished waiting for docker on all instances")

        # XXX I guess we should return here and let the test happen?
        # looks like we're reaping the instance right away

    def release_run(self, **options):
        # return the instances to the pool
        run_id = options['run_id']
        uuid = options['uuid']
        region = options.get('region', 'us-west-2')

        yield self.pool.release_run(run_id, uuid, region)

        # reap the pool
        logger.debug("Reaping instances...")
        yield self.pool.reap_instances()
        logger.debug("Finished terminating.")

        # mark the state in the DB
        session = self.db.session()
        run = session.query(Run).filter(Run.run_id==run_id).one()
        run.state = COMPLETED
        session.commit()

        logger.debug("Finished test run, all cleaned up.")

    def run_test(self, **options):
        nodes = options.pop('nodes')
        options.pop("user_data")

        collection_uuid = str(uuid4())

        # creating the whole DB structure
        run = Run(**options)
        session = self.db.session()
        session.add(run)
        session.commit()

        callback = partial(self._test, run, session)
        logger.debug("requesting instances")

        self.pool.request_instances(
            run.uuid, collection_uuid, count=int(nodes),
            inst_type="t1.micro", callback=callback)

        # what about regions
        return run.uuid, collection_uuid, 'us-west-2'


class RunManager:
    """Manages the life-cycle of a load run.

    """
    def __init__(self):
        pass

    @classmethod
    def recover_run(cls, uuid):
        pass

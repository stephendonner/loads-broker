import unittest
# from tornado.testing import AsyncTestCase, gen_test
import boto
from moto import mock_ec2

from freezegun import freeze_time


class Test_populate_ami_ids(unittest.TestCase):
    def tearDown(self):
        import loadsbroker.aws
        loadsbroker.aws.AWS_AMI_IDS = {k: {} for k in
                                       loadsbroker.aws.AWS_REGIONS}

    @mock_ec2
    def test_no_instances(self):
        import loadsbroker.aws
        self.assertEqual(len(loadsbroker.aws.AWS_AMI_IDS),
                         len(loadsbroker.aws.AWS_REGIONS))
        first_region = loadsbroker.aws.AWS_REGIONS[0]
        self.assertEqual(len(loadsbroker.aws.AWS_AMI_IDS[first_region]), 0)
        loadsbroker.aws.populate_ami_ids(use_filters=False)
        self.assertEqual(len(loadsbroker.aws.AWS_AMI_IDS[first_region]), 0)

    @mock_ec2
    def test_ami_is_found(self):
        import loadsbroker.aws
        first_region = loadsbroker.aws.AWS_REGIONS[0]
        self.assertEqual(len(loadsbroker.aws.AWS_AMI_IDS[first_region]), 0)

        # Populate a few instances into the mock ec2
        conn = boto.ec2.connect_to_region(first_region)
        reservation = conn.run_instances('ami-1234abcd')
        instance = reservation.instances[0]
        conn.create_image(instance.id, "CoreOS stable")

        loadsbroker.aws.populate_ami_ids(use_filters=False)
        self.assertEqual(len(loadsbroker.aws.AWS_AMI_IDS[first_region]), 1)


class Test_get_ami(unittest.TestCase):
    def tearDown(self):
        import loadsbroker.aws
        loadsbroker.aws.AWS_AMI_IDS = {k: {} for k in
                                       loadsbroker.aws.AWS_REGIONS}

    @mock_ec2
    def test_image_found(self):
        import loadsbroker.aws
        first_region = loadsbroker.aws.AWS_REGIONS[0]

        # Stash an image for a m1.small
        conn = boto.ec2.connect_to_region(first_region)
        reservation = conn.run_instances("ami-1234abcd",
                                         instance_type='m1.small')
        instance = reservation.instances[0]
        image_id = conn.create_image(instance.id, "CoreOS stable")
        img = conn.get_all_images()[0]

        loadsbroker.aws.AWS_AMI_IDS[first_region] = {"paravirtual": img}

        ami_id = loadsbroker.aws.get_ami(first_region, "m1.small")
        self.assertEqual(ami_id, image_id)

    def test_no_image_found(self):
        import loadsbroker.aws
        first_region = loadsbroker.aws.AWS_REGIONS[0]
        self.assertRaises(KeyError, loadsbroker.aws.get_ami, first_region,
                          "m1.small")


class Test_available_instance(unittest.TestCase):
    def _callFUT(self, instance):
        from loadsbroker.aws import available_instance
        return available_instance(instance)

    @mock_ec2
    def test_running_instance_usable(self):
        # Setup a running instance
        conn = boto.connect_ec2()
        conn.run_instances("ami-1234abcd", instance_type='m1.small')
        reservations = conn.get_all_instances()
        instance = reservations[0].instances[0]

        self.assertTrue(self._callFUT(instance))

    @mock_ec2
    def test_pending_instance_usable(self):
        with freeze_time("2012-01-14 03:21:34"):
            # Setup a running instance
            conn = boto.connect_ec2('the_key', 'the_secret')
            reservation = conn.run_instances("ami-1234abcd",
                                             instance_type='m1.small')
            instance = reservation.instances[0]

        with freeze_time("2012-01-14 03:22:34"):
            self.assertTrue(self._callFUT(instance))

    @mock_ec2
    def test_pending_instance_unusable(self):
        # Setup a running instance
        with freeze_time("2012-01-14 03:21:34"):
            conn = boto.connect_ec2()
            reservation = conn.run_instances("ami-1234abcd",
                                             instance_type='m1.small')
            instance = reservation.instances[0]

        with freeze_time("2012-01-14 03:24:34"):
            self.assertFalse(self._callFUT(instance))

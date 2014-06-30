#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
Agent pytest tests.
'''
import os
from datetime import datetime
from multiprocessing.queues import Empty

import requests
from mock import MagicMock
from interruptingcow import timeout

from whmonit.common.types import SensorConfig
from whmonit.common.webclient import RequestManager
from whmonit.client.agent import Agent, Shipper, AgentInternal, Receiver, Sensor
from whmonit.client.sensors.uptime.linux_01 import Sensor as UptimeSensor
from whmonit.common.test.helpers import UnbufferedNamedTemporaryFile


# W0212: Access to a protected member of a client class
# W0201: Attribute defined outside __init__
# pylint: disable=W0212,W0201

class TestAgent(object):
    '''
    Agent tests.
    '''

    def setup(self):
        '''
        Setup test.
        '''
        self.agentconfig = UnbufferedNamedTemporaryFile('sensors: []', False)
        self.agent = Agent(
            self.agentconfig.name,
            'dzik1',
            'http://localhost:8000',
            'ws://localhost:8080',
            '.agentdata.db',
            os.path.dirname(os.path.realpath(__file__))
        )
        self.agent.make_request = MagicMock()
        self.agent.get_remote_config = MagicMock()
        self.agent._start_subprocess = MagicMock()
        self.agent._restart_subprocess = MagicMock()
        self.agent._restart_subprocess.side_effect = Exception

        self.csr_file = UnbufferedNamedTemporaryFile('csr content', False)
        self.crt_file = UnbufferedNamedTemporaryFile('crt content', False)
        self.key_file = UnbufferedNamedTemporaryFile('key content', False)
        self.ca_file = UnbufferedNamedTemporaryFile('ca content', False)
        self.agent.csr_path = self.csr_file.name
        self.agent.crt_path = self.crt_file.name
        self.agent.key_path = self.key_file.name
        self.agent.ca_path = self.ca_file.name

    def teardown(self):
        '''
        Teardown test.
        '''
        self.agentconfig.close()
        self.csr_file.close()
        self.crt_file.close()
        self.key_file.close()
        self.ca_file.close()

    def test_request_certificate(self):
        '''
        Request certificate.
        '''
        self.agent.request_certificate()
        self.agent.make_request.assert_called_once_with(
            requests.put,
            'http://localhost:8000/csr',
            'csr content'
        )

    def test_fetch_certificate(self):
        '''
        Fetch certificate.
        '''
        RequestManager.__init__ = MagicMock()
        RequestManager.__init__.return_value = None
        RequestManager.request = MagicMock()
        RequestManager.request.return_value = 'new crt'
        RequestManager.close = MagicMock()

        self.agent.fetch_certificate()
        RequestManager.request.assert_called_once_with(
            'certificates',
            'fetch',
            {'agent_id': 'dzik1'}
        )
        assert open(self.crt_file.name).read() == 'new crt'

    def test_run_no_sensrs(self):
        '''
        Run with no sensors.
        '''
        try:
            with timeout(1.5):
                self.agent.run()
        except RuntimeError:
            pass

        assert self.agent._start_subprocess.call_count == 2

    def test_run_one_sensor(self):
        '''
        Run with one sensor - uptime.
        '''
        self.agent.agentconfig = {'sensors': [{
            'config': {'frequency': 60},
            'sensor': 'uptime',
            'config_id': 'config_id',
            'target': 'target',
            'target_id': 'target_id'
        }]}

        try:
            with timeout(2.5):
                self.agent.run()
        except RuntimeError:
            pass

        assert self.agent._start_subprocess.call_count == 3


class TestShipper(object):
    '''
    Shipper tests.
    '''

    def setup(self):
        '''
        Setup test.
        '''
        self.send_results = MagicMock()
        AgentInternal._prepare_sqlite = MagicMock()
        AgentInternal.assert_parent_exists = MagicMock()
        AgentInternal.run = MagicMock()
        self.shipper = Shipper(':memory:', self.send_results)
        self.shipper.sqliteconn = MagicMock()

    def test_run_no_data(self):
        '''
        Run without upcoming data.
        '''
        try:
            with timeout(1.5):
                self.shipper.run()
        except RuntimeError:
            pass

        assert self.shipper._prepare_sqlite.called
        assert self.shipper.sqliteconn.cursor.called
        assert self.shipper.assert_parent_exists.called


class TestReceiver(object):
    '''
    Receiver tests.
    '''

    def setup(self):
        '''
        Setup test.
        '''
        self.send_results = MagicMock()
        AgentInternal._prepare_sqlite = MagicMock()
        AgentInternal.assert_parent_exists = MagicMock()
        AgentInternal.run = MagicMock()
        self.receiver = Receiver(':memory:', self.send_results)
        self.receiver.queue = MagicMock()
        self.receiver.queue.get_nowait.side_effect = Empty
        self.receiver.serializer = MagicMock()

    def test_run_no_data(self):
        '''
        Run without upcoming data.
        '''
        try:
            with timeout(1.5):
                self.receiver.run()
        except RuntimeError:
            pass

        assert self.receiver._prepare_sqlite.called
        assert self.receiver.sqliteconn.commit.called
        assert self.receiver.assert_parent_exists.called


class TestSensor(object):
    '''
    Sensor tests.
    '''

    def setup(self):
        '''
        Setup test.
        '''
        Sensor.pid = 1
        self.queue = MagicMock()
        self.sensor = Sensor(
            self.queue,
            'uptime',
            SensorConfig({'frequency': 1}),
            'config_id',
            'target',
            'target_id'
        )

    def test_run(self):
        '''
        Run sensor.
        '''
        self.sensor.send_results = MagicMock()
        try:
            with timeout(1.5):
                self.sensor.run()
        except RuntimeError:
            pass

        assert self.sensor.send_results.called

    def test_send_results(self):
        '''
        Send valid results.
        '''
        timestamp = datetime.now()
        self.sensor.send_results(
            UptimeSensor,
            timestamp,
            [('default', 1.1)]
        )
        self.queue.put.assert_called_once_with({
            'datatype': float,
            'timestamp': timestamp,
            'config_id': 'config_id',
            'data': 1.1,
            'stream_name': 'default'
        })

    def test_send_results_bad_stream(self):
        '''
        Send results with wrong stream name.
        '''
        timestamp = datetime.now()
        self.sensor.send_results(
            UptimeSensor,
            timestamp,
            [('badstream', 1.1)]
        )
        assert self.queue.put.call_count == 0

    def test_send_results_bad_datatype(self):
        '''
        Send results with wrong data type.
        '''
        timestamp = datetime.now()
        self.sensor.send_results(
            UptimeSensor,
            timestamp,
            [('default', 1)]
        )
        assert self.queue.put.call_count == 0

    def test_send_results_bad_type(self):
        '''
        Send results with data type not in TYPE_REGISTRY.
        '''
        UptimeSensor.streams['badstream'] = int
        timestamp = datetime.now()
        self.sensor.send_results(
            UptimeSensor,
            timestamp,
            [('badstream', 1)]
        )
        assert self.queue.put.call_count == 0

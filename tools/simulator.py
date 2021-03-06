#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import eventlet
eventlet.monkey_patch()

import argparse
import collections
import datetime
import logging
import os
import random
import string
import sys
import threading
import time
import yaml

from scipy.stats import rv_discrete

from oslo_config import cfg
import oslo_messaging as messaging
from oslo_messaging import notify  # noqa
from oslo_messaging import rpc  # noqa

LOG = logging.getLogger()
RANDOM_VARIABLE = None
CURRENT_PID = None
RPC_CLIENTS = []
MESSAGES = []

USAGE = """ Usage: ./simulator.py [-h] [--url URL] [-d DEBUG]\
 {notify-server,notify-client,rpc-server,rpc-client} ...

Usage example:
 python tools/simulator.py\
 --url rabbit://stackrabbit:secretrabbit@localhost/ rpc-server
 python tools/simulator.py\
 --url rabbit://stackrabbit:secretrabbit@localhost/ rpc-client\
 --exit-wait 15000 -p 64 -m 64"""


def init_random_generator():
    data = []
    with open('./messages_length.yaml') as m_file:
        content = yaml.load(m_file)
        data += [int(n) for n in content[
            'test_data']['string_lengths'].split(', ')]

    ranges = collections.defaultdict(int)
    for msg_length in data:
        range_start = (msg_length / 500) * 500 + 1
        ranges[range_start] += 1

    ranges_start = sorted(ranges.keys())
    total_count = len(data)
    ranges_dist = []
    for r in ranges_start:
        r_dist = float(ranges[r]) / total_count
        ranges_dist.append(r_dist)

    random_var = rv_discrete(values=(ranges_start, ranges_dist))
    return random_var


class LoggingNoParsingFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        for i in ['received {', 'MSG_ID is ']:
            if i in msg:
                return False
        return True


class Monitor(object):
    def __init__(self, show_stats=False, *args, **kwargs):
        self._count = self._prev_count = 0
        self.show_stats = show_stats
        if self.show_stats:
            self._monitor()

    def _monitor(self):
        threading.Timer(1.0, self._monitor).start()
        LOG.debug("%d msg was received per second",
                  (self._count - self._prev_count))
        self._prev_count = self._count

    def info(self, *args, **kwargs):
        self._count += 1


class NotifyEndpoint(Monitor):
    def __init__(self, *args, **kwargs):
        super(NotifyEndpoint, self).__init__(*args, **kwargs)
        self.cache = []

    def info(self, ctxt, publisher_id, event_type, payload, metadata):
        super(NotifyEndpoint, self).info(ctxt, publisher_id, event_type,
                                         payload, metadata)
        LOG.debug('msg rcv')
        LOG.debug("%s %s %s %s", ctxt, publisher_id, event_type, payload)
        if not self.show_stats and payload not in self.cache:
            LOG.debug('requeue msg')
            self.cache.append(payload)
            for i in range(15):
                eventlet.sleep(1)
            return messaging.NotificationResult.REQUEUE
        else:
            LOG.debug('ack msg')
        return messaging.NotificationResult.HANDLED


def notify_server(transport, show_stats):
    endpoints = [NotifyEndpoint(show_stats)]
    target = messaging.Target(topic='n-t1')
    server = notify.get_notification_listener(transport, [target],
                                              endpoints, executor='eventlet')
    server.start()
    server.wait()


class BatchNotifyEndpoint(Monitor):
    def __init__(self, *args, **kwargs):
        super(BatchNotifyEndpoint, self).__init__(*args, **kwargs)
        self.cache = []

    def info(self, messages):
        super(BatchNotifyEndpoint, self).info(messages)
        self._count += len(messages) - 1

        LOG.debug('msg rcv')
        LOG.debug("%s", messages)
        if not self.show_stats and messages not in self.cache:
            LOG.debug('requeue msg')
            self.cache.append(messages)
            for i in range(15):
                eventlet.sleep(1)
            return messaging.NotificationResult.REQUEUE
        else:
            LOG.debug('ack msg')
        return messaging.NotificationResult.HANDLED


def batch_notify_server(transport, show_stats):
    endpoints = [BatchNotifyEndpoint(show_stats)]
    target = messaging.Target(topic='n-t1')
    server = notify.get_batch_notification_listener(
        transport, [target],
        endpoints, executor='eventlet',
        batch_size=1000, batch_time=5)
    server.start()
    server.wait()


class RpcEndpoint(Monitor):
    def __init__(self, wait_before_answer, show_stats):
        self.count = None
        self.wait_before_answer = wait_before_answer

    def info(self, ctxt, message):
        i = int(message.split(' ')[-1])
        if self.count is None:
            self.count = i
        elif i == 0:
            self.count = 0
        else:
            self.count += 1

        LOG.debug("######## RCV: %s/%s", self.count, message)
        if self.wait_before_answer > 0:
            time.sleep(self.wait_before_answer)
        return "OK: %s" % message


class RPCClient(object):
    def __init__(self, transport, target, timeout, method, wait_after_msg):
        self.client = rpc.RPCClient(transport, target)
        self.client.prepare(timeout=timeout)
        self.method = method
        self.bytes = 0
        self.msg_sent = 0
        self.messages_count = len(MESSAGES)
        # Start sending the messages from a random position to avoid
        # memory re-usage and generate more realistic load on the library
        # and a message transport
        self.position = random.randint(0, self.messages_count - 1)
        self.wait_after_msg = wait_after_msg

    def send_msg(self):
        msg = MESSAGES[self.position]
        self.method(self.client, msg)
        self.bytes += len(msg)
        self.msg_sent += 1
        self.position = (self.position + 1) % self.messages_count
        if self.wait_after_msg > 0:
            time.sleep(self.wait_after_msg)


def init_msg(messages_count):
    # Limit the messages amount. Clients will reiterate the array again
    # if an amount of messages to be sent is bigger than 1000
    if messages_count > 1000:
        messages_count = 1000
    LOG.info("Preparing %d messages", messages_count)
    ranges = RANDOM_VARIABLE.rvs(size=messages_count)
    i = 0
    for range_start in ranges:
        length = random.randint(range_start, range_start + 497)
        msg = ''.join(random.choice(string.lowercase) for x in range(length)) \
              + ' ' + str(i)
        MESSAGES.append(msg)
        i += 1
    LOG.info("Messages has been prepared")


def rpc_server(transport, target, wait_before_answer, executor, show_stats):
    endpoints = [RpcEndpoint(wait_before_answer, show_stats)]
    server = rpc.get_rpc_server(transport, target, endpoints,
                                executor=executor)
    server.start()
    server.wait()


def threads_spawner(threads, method, *args, **kwargs):
    p = eventlet.GreenPool(size=threads)
    for i in range(0, threads):
        p.spawn_n(method, i, *args, **kwargs)
    p.waitall()


def send_msg(c_id, transport, target, wait_after_msg, timeout, is_cast,
             messages_count):
    LOG.debug("Sending %d messages using client %d", messages_count, c_id)
    rpc_method = _rpc_cast if is_cast else _rpc_call
    client = RPCClient(transport, target, timeout, rpc_method, wait_after_msg)
    RPC_CLIENTS.append(client)
    for _ in xrange(0, messages_count):
        client.send_msg()
    LOG.debug("Client %d has sent all messages", c_id)


def _rpc_call(client, msg):
    try:
        res = client.call({}, 'info', message=msg)
    except Exception as e:
        LOG.exception('Error %s on CALL for message %s', str(e), msg)
    else:
        LOG.debug("SENT: %s, RCV: %s", msg, res)


def _rpc_cast(client, msg):
    try:
        client.cast({}, 'info', message=msg)
    except Exception as e:
        LOG.exception('Error %s on CAST for message %s', str(e), msg)
    else:
        LOG.debug("SENT: %s", msg)


def notifier(_id, transport, messages, wait_after_msg, timeout):
    n1 = notify.Notifier(transport, topic="n-t1").prepare(
        publisher_id='publisher-%d' % _id)
    msg = 0
    for i in range(0, messages):
        msg = 1 + msg
        ctxt = {}
        payload = dict(msg=msg, vm='test', otherdata='ahah')
        LOG.debug("send msg")
        LOG.debug(payload)
        n1.info(ctxt, 'compute.start1', payload)
        if wait_after_msg > 0:
            time.sleep(wait_after_msg)


def _setup_logging(is_debug):
    log_level = logging.DEBUG if is_debug else logging.INFO
    logging.basicConfig(stream=sys.stdout, level=log_level)
    logging.getLogger().handlers[0].addFilter(LoggingNoParsingFilter())
    for i in ['kombu', 'amqp', 'stevedore', 'qpid.messaging'
              'oslo.messaging._drivers.amqp', ]:
        logging.getLogger(i).setLevel(logging.WARN)


def main():
    parser = argparse.ArgumentParser(
        description='Tools to play with oslo.messaging\'s RPC',
        usage=USAGE,
    )
    parser.add_argument('--url', dest='url',
                        default='rabbit://guest:password@localhost/',
                        help="oslo.messaging transport url")
    parser.add_argument('-d', '--debug', dest='debug', type=bool,
                        default=False,
                        help="Turn on DEBUG logging level instead of WARN")
    parser.add_argument('-tp', '--topic', dest='topic',
                        default="profiler_topic",
                        help="Topic to publish/receive messages to/from.")
    subparsers = parser.add_subparsers(dest='mode',
                                       help='notify/rpc server/client mode')

    server = subparsers.add_parser('notify-server')
    server.add_argument('--show-stats', dest='show_stats',
                        type=bool, default=True)
    server = subparsers.add_parser('batch-notify-server')
    server.add_argument('--show-stats', dest='show_stats',
                        type=bool, default=True)
    client = subparsers.add_parser('notify-client')
    client.add_argument('-p', dest='threads', type=int, default=1,
                        help='number of client threads')
    client.add_argument('-m', dest='messages', type=int, default=1,
                        help='number of call per threads')
    client.add_argument('-w', dest='wait_after_msg', type=int, default=-1,
                        help='sleep time between two messages')
    client.add_argument('-t', dest='timeout', type=int, default=3,
                        help='client timeout')

    server = subparsers.add_parser('rpc-server')
    server.add_argument('-w', dest='wait_before_answer', type=int, default=-1)
    server.add_argument('--show-stats', dest='show_stats',
                        type=bool, default=True)
    server.add_argument('-e', '--executor', dest='executor',
                        type=str, default='eventlet',
                        help='name of a message executor')

    client = subparsers.add_parser('rpc-client')
    client.add_argument('-p', dest='threads', type=int, default=1,
                        help='number of client threads')
    client.add_argument('-m', dest='messages', type=int, default=1,
                        help='number of call per threads')
    client.add_argument('-w', dest='wait_after_msg', type=int, default=-1,
                        help='sleep time between two messages')
    client.add_argument('-t', dest='timeout', type=int, default=3,
                        help='client timeout')
    client.add_argument('--exit-wait', dest='exit_wait', type=int, default=0,
                        help='Keep connections open N seconds after calls '
                        'have been done')
    client.add_argument('--is-cast', dest='is_cast', type=bool, default=False,
                        help='Use `call` or `cast` RPC methods')

    args = parser.parse_args()

    _setup_logging(is_debug=args.debug)

    if args.mode in ['rpc-server', 'rpc-client']:
        transport = messaging.get_transport(cfg.CONF, url=args.url)
    else:
        transport = messaging.get_notification_transport(cfg.CONF,
                                                         url=args.url)
        cfg.CONF.oslo_messaging_notifications.topics = "notif"
        cfg.CONF.oslo_messaging_notifications.driver = "messaging"
    target = messaging.Target(topic=args.topic, server='profiler_server')

    # oslo.config defaults
    cfg.CONF.heartbeat_interval = 5
    cfg.CONF.prog = os.path.basename(__file__)
    cfg.CONF.project = 'oslo.messaging'

    if args.mode == 'rpc-server':
        if args.url.startswith('zmq'):
            cfg.CONF.rpc_zmq_matchmaker = "redis"
            transport._driver.matchmaker._redis.flushdb()
        rpc_server(transport, target, args.wait_before_answer, args.executor,
                   args.show_stats)
    elif args.mode == 'notify-server':
        notify_server(transport, args.show_stats)
    elif args.mode == 'batch-notify-server':
        batch_notify_server(transport, args.show_stats)
    elif args.mode == 'notify-client':
        threads_spawner(args.threads, notifier, transport, args.messages,
                        args.wait_after_msg, args.timeout)
    elif args.mode == 'rpc-client':
        init_msg(args.messages)

        start = datetime.datetime.now()
        threads_spawner(args.threads, send_msg, transport, target,
                        args.wait_after_msg, args.timeout, args.is_cast,
                        args.messages)
        time_elapsed = (datetime.datetime.now() - start).total_seconds()

        msg_count = 0
        total_bytes = 0
        for client in RPC_CLIENTS:
            msg_count += client.msg_sent
            total_bytes += client.bytes

        LOG.info('%d messages were sent for %d seconds. '
                 'Bandwidth was %d msg/sec', msg_count, time_elapsed,
                 (msg_count / time_elapsed))
        log_msg = '%s bytes were sent for %d seconds. Bandwidth is %d b/s' % (
            total_bytes, time_elapsed, (total_bytes / time_elapsed))
        LOG.info(log_msg)
        with open('./oslo_res_%s.txt' % args.topic, 'a+') as f:
            f.write(log_msg + '\n')

        LOG.info("calls finished, wait %d seconds" % args.exit_wait)
        time.sleep(args.exit_wait)


if __name__ == '__main__':
    RANDOM_VARIABLE = init_random_generator()
    CURRENT_PID = os.getpid()
    main()

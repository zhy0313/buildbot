# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members
from __future__ import print_function

import StringIO
import mock
import os
import sys
import textwrap

from twisted.internet import defer
from twisted.internet import reactor
from twisted.internet import task
from twisted.trial import unittest

from buildbot.master import BuildMaster
from buildbot.schedulers.basic import BaseBasicScheduler
from buildbot.status.results import SUCCESS
from buildbot.status.results import statusToString
from buildbot.test.util import dirs
from buildslave.bot import BuildSlave
from buildslave.bot import LocalBuildSlave


class WaitForStartConsumingSchedulers(object):

    """
    startService does not return a deferred. So, when
    we call yield master.reconfigServiceWithBuildbotConfig
    we cannot know whether services, like schedulers, have finished their
    setup.
    This creates an issue regarding the SingleBranch, AnyBranch schedulers.
    These schedulers are able to trigger builds only after they have
    subscribed to the 'new' change event. If we try to start a build with a new change
    before they subscribed to the event (what can happen as startService does not return a deferred),
    the build will never start, and the test will timeout.
    This class implements a poller to ensure that these schedulers have subscribed to the event,
    before any test tries to create a build with a new change.
    """
    POLL_INTERVAL_SEC = 1

    def __init__(self, master):
        self.ds = [(defer.Deferred(), s)
                   for s in master.allSchedulers()
                   if isinstance(s, BaseBasicScheduler) and s._change_consumer is None]
        self.task = None

    def startPolling(self):
        self.task = task.LoopingCall(self.callbackSchedulers)
        self.task.start(self.POLL_INTERVAL_SEC, now=True)

    def stopPolling(self):
        self.task.stop()

    def getAllDeferreds(self):
        return [d for (d, _) in self.ds]

    def callbackSchedulers(self):
        for (d, s) in self.ds:
            if s._change_consumer is not None:
                d.callback(None)
        self.ds = [(d, s) for (d, s) in self.ds
                   if s._change_consumer is None]

    @defer.inlineCallbacks
    def waitForSchedToBeActive(self):
        d = defer.DeferredList(self.getAllDeferreds())
        self.startPolling()
        yield d
        self.stopPolling()


class RunMasterBase(dirs.DirsMixin, unittest.TestCase):
    proto = "null"
    # If True the test cases must handle the configuration
    # of the master in the self.master attribute themselves.
    # The setupConfig could help the module in that task.
    # Note that whether testCaseHandleTheirSetup is False or True
    # in all cases, tearDown that stops the master defined in self.master
    # will be called.
    testCasesHandleTheirSetup = False

    @defer.inlineCallbacks
    def setupConfig(self, configFunc):
        """
        Setup and start a master configured
        by the function configFunc defined in the test module.
        @type configFunc: string
        @param configFunc: name of a function
        without argument defined in the test module
        that returns a BuildmasterConfig object.
        """
        self.basedir = os.path.abspath('basdir')
        self.setUpDirs(self.basedir)
        self.configfile = os.path.join(self.basedir, 'master.cfg')
        if self.proto == 'pb':
            proto = '{"pb": {"port": "tcp:0:interface=127.0.0.1"}}'
        elif self.proto == 'null':
            proto = '{"null": {}}'
        # We create a master.cfg, which loads the configuration from the
        # test module. Only the slave config is kept there, as it should not
        # be changed
        open(self.configfile, "w").write(textwrap.dedent("""
            from buildbot.buildslave import BuildSlave
            from %s import %s
            c = BuildmasterConfig = %s()
            c['slaves'] = [BuildSlave("local1", "localpw")]
            c['protocols'] = %s
            """ % (self.__class__.__module__,
                   configFunc, configFunc,
                   proto)))
        # create the master and set its config
        m = BuildMaster(self.basedir, self.configfile)
        self.master = m

        # update the DB
        yield m.db.setup(check_version=False)
        yield m.db.model.upgrade()

        # stub out m.db.setup since it was already called above
        m.db.setup = lambda: None

        # mock reactor.stop (which trial *really* doesn't
        # like test code to call!)
        mock_reactor = mock.Mock(spec=reactor)
        mock_reactor.callWhenRunning = reactor.callWhenRunning

        # start the service
        yield m.startService(_reactor=mock_reactor)
        self.failIf(mock_reactor.stop.called,
                    "startService tried to stop the reactor; check logs")

        if self.proto == 'pb':
            # We find out the slave port automatically
            slavePort = m.pbmanager.dispatchers.values()[0].port.getHost().port

            # create a slave, and attach it to the master, it will be started, and stopped
            # along with the master
            s = BuildSlave("127.0.0.1", slavePort, "local1", "localpw", self.basedir, False, False)
        elif self.proto == 'null':
            s = LocalBuildSlave("local1", self.basedir, False)
        s.setServiceParent(m)
        ws = WaitForStartConsumingSchedulers(self.master)
        yield ws.waitForSchedToBeActive()

    def setUp(self):
        if self.testCasesHandleTheirSetup:
            return defer.succeed(None)
        return self.setupConfig("masterConfig")

    @defer.inlineCallbacks
    def tearDown(self):
        if not self._passed:
            dump = StringIO.StringIO()
            print >> dump, "FAILED! dumping build db for debug"
            builds = yield self.master.data.get(("builds",))
            for build in builds:
                yield self.printBuild(build, dump)
        m = self.master
        # stop the service
        yield m.stopService()

        # and shutdown the db threadpool, as is normally done at reactor stop
        m.db.pool.shutdown()

        # (trial will verify all reactor-based timers have been cleared, etc.)
        self.tearDownDirs()
        if not self._passed:
            raise self.failureException(dump.getvalue())

    @defer.inlineCallbacks
    def doForceBuild(self, wantSteps=False, wantProperties=False,
                     wantLogs=False, useChange=False):

        # force a build, and wait until it is finished
        d = defer.Deferred()

        # in order to allow trigger based integration tests
        # we wait until the first started build is finished
        self.firstBuildId = None

        def newCallback(_, data):
            if self.firstBuildId is None:
                self.firstBuildId = data['buildid']
                newConsumer.stopConsuming()

        def finishedCallback(_, data):
            if self.firstBuildId == data['buildid']:
                d.callback(data)

        newConsumer = yield self.master.mq.startConsuming(
            newCallback,
            ('builds', None, 'new'))

        finishedConsumer = yield self.master.mq.startConsuming(
            finishedCallback,
            ('builds', None, 'finished'))

        if useChange is False:
            # use data api to force a build
            yield self.master.data.control("force", {}, ("forceschedulers", "force"))
        else:
            # use data api to force a build, via a new change
            yield self.master.data.updates.addChange(**useChange)

        # wait until we receive the build finished event
        build = yield d
        finishedConsumer.stopConsuming()
        yield self.enrichBuild(build, wantSteps, wantProperties, wantLogs)
        defer.returnValue(build)

    @defer.inlineCallbacks
    def enrichBuild(self, build, wantSteps=False, wantProperties=False, wantLogs=False):
        # enrich the build result, with the step results
        if wantSteps:
            build["steps"] = yield self.master.data.get(("builds", build['buildid'], "steps"))
            # enrich the step result, with the logs results
            if wantLogs:
                build["steps"] = list(build["steps"])
                for step in build["steps"]:
                    step['logs'] = yield self.master.data.get(("steps", step['stepid'], "logs"))
                    step["logs"] = list(step['logs'])
                    for log in step["logs"]:
                        log['contents'] = yield self.master.data.get(("logs", log['logid'], "contents"))

        if wantProperties:
            build["properties"] = yield self.master.data.get(("builds", build['buildid'], "properties"))

    @defer.inlineCallbacks
    def printBuild(self, build, out=sys.stdout):
        # helper for debugging: print a build
        yield self.enrichBuild(build, wantSteps=True, wantProperties=True, wantLogs=True)
        print("*** BUILD %d *** ==> %s (%s)" % (build['buildid'], build['state_string'],
              statusToString(build['results'])), file=out)
        for step in build['steps']:
            print("    *** STEP %s *** ==> %s (%s)" % (step['name'], step['state_string'],
                  statusToString(step['results'])), file=out)
            for url in step['urls']:
                print("       url:%s (%s)" % (url['name'], url['url']), file=out)
            for log in step['logs']:
                print("        log:%s (%d)" % (log['name'], log['num_lines']), file=out)
                if step['results'] != SUCCESS:
                    self.printLog(log, out)

    def printLog(self, log, out):
        print(" " * 8 + "*********** LOG: %s *********" % (log['name'],), file=out)
        if log['type'] == 's':
            for line in log['contents']['content'].splitlines():
                linetype = line[0]
                line = line[1:]
                if linetype == 'h':
                    # cyan
                    line = "\x1b[36m" + line + "\x1b[0m"
                if linetype == 'e':
                    # red
                    line = "\x1b[31m" + line + "\x1b[0m"
                print(" " * 8 + line)
        else:
            print(log['contents']['content'], file=out)
        print(" " * 8 + "********************************", file=out)

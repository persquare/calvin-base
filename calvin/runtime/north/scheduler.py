# -*- coding: utf-8 -*-

# Copyright (c) 2015 Ericsson AB
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import time
import random
import logging

from monitor import Event_Monitor, VisualizingMonitor
from calvin.runtime.south.plugins.async import async
from calvin.utilities.calvin_callback import CalvinCB
from calvin.utilities.calvinlogger import get_logger
from calvin.utilities import calvinconfig

_log = get_logger(__name__)
_conf = calvinconfig.get()

class BaseScheduler(object):

    """
    The scheduler is the only active component in a runtime,
    except for calvinsys and token transport. Every other piece of
    code is (or should be) purely reactive.

    This class cannot be used as a scheduler
    """

    def __init__(self, node, actor_mgr):
        super(BaseScheduler, self).__init__()
        self.node = node
        self.actor_mgr = actor_mgr
        self.done = False
        self._tasks = []
        self._scheduled = None
        # FIXME: later
        self._replication_interval = 2
        self._maintenance_delay = _conf.get(None, "maintenance_delay") or 300

    # System entry point
    def run(self):
        self.insert_task(self._maintenance_loop, self._maintenance_delay)
        self.insert_task(self._check_replication, self._replication_interval)
        self.insert_task(self.strategy, 0)
        async.run_ioloop()

    # System exit point
    def stop(self):
        if not self.done:
            async.DelayedCall(0, async.stop_ioloop)
        self.done = True

    ######################################################################
    # Event "API" used by subsystems to inform scheduler about events
    # Most of them needs to be subclassed in a working scheduler
    ######################################################################

    def tunnel_rx(self, endpoint):
        """Token recieved on endpoint"""
        # We got a token, meaning that the corrsponding actor could possibly fire
        pass

    def tunnel_tx_ack(self, endpoint):
        """Token successfully sent on endpoint"""
        # We got back ACK on sent token; at least one slot free in out queue, endpoint can send again at any time
        pass

    def tunnel_tx_nack(self, endpoint):
        """Token unsuccessfully sent on endpoint"""
        # We got back NACK on sent token, endpoint should wait before resending
        pass

    def tunnel_tx_throttle(self, endpoint):
        """Backoff request for endpoint"""
        # Schedule tx for endpoint at this time
        # FIXME: Under what circumstances is this method called?
        pass

    def schedule_calvinsys(self, actor_id=None):
        """Incoming platform event"""
        pass

    def register_endpoint(self, endpoint):
        pass

    def unregister_endpoint(self, endpoint):
        pass

    ######################################################################
    # Stuff that needs to be implemented in a subclass
    ######################################################################

    def strategy(self):
        """This is where the scheduling happens..."""
        raise Exception("Really need a strategy")

    def watchdog(self):
        """If nothing else is scheduled, this will be called after 60s"""
        pass

    ######################################################################
    # Semi-private stuff, should be cleaned up later
    ######################################################################

    #
    # Replication
    #
    def _check_replication(self):
        # Control replication
        self.node.rm.replication_loop()
        self.insert_task(self.strategy, 0)
        self.insert_task(self._check_replication, self._replication_interval)

    #
    # Maintenance loop
    #
    def _maintenance_loop(self):
        # Migrate denied actors
        for actor in self.actor_mgr.migratable_actors():
            self.actor_mgr.migrate(actor.id, actor.migration_info["node_id"],
                                   callback=CalvinCB(actor.remove_migration_info))
        # Enable denied actors again if access is permitted. Will try to migrate if access still denied.
        for actor in self.actor_mgr.denied_actors():
            actor.enable_or_migrate()
        # TODO: try to migrate shadow actors as well.
        # Since we may have moved stuff around, schedule strategy
        self.insert_task(self.strategy, 0)
        # Schedule next maintenance
        self.insert_task(self._maintenance_loop, self._maintenance_delay)

    def trigger_maintenance_loop(self, delay=False):
        """Public API"""
        if delay:
            # No need to schedule delayed maintenance, we do that periodically anyway
            return
        self.insert_task(self._maintenance_loop, 0)

    ######################################################################
    # Quite-private stuff, fairly generic
    ######################################################################

    def insert_task(self, what, delay):
        """Call to insert a task"""
        # Insert a task in time order,
        # if it ends up first in list, re-schedule _process_next
        t = time.time() + delay
        # task is (time, func)
        task = (t, what)
        index = len(self._tasks)
        if index:
            for i, ti in enumerate(self._tasks):
                if ti[0] > t:
                    index = i
                    break
        # If list was empty => index = 0
        # If slot found => index is insertion point
        # If slot not found => index is length of list <=> append
        # coalesce => don't add a task b/c we already will do that
        coalesce = (index > 0 and delay == 0 and self._tasks[index-1][1] == what)
        if coalesce:
            return
        self._tasks.insert(index, task)
        # print "INSERTING TASK AT SLOT", index
        # print "TASKS:", [(t, f.__name__) for t, f in self._tasks]
        # If we're first, reschedule
        if index == 0:
            self._schedule_next(delay, self._process_next)

    # Don't call directly
    def _schedule_next(self, delay, what):
        if self._scheduled:
            self._scheduled.cancel()
        self._scheduled = async.DelayedCall(delay, what)

    # Don't call directly
    def _process_next(self):
        # Get next task from queue and do it unless next task is in the future,
        # in that case, schedule _process_next (this method) at that time
        _, todo = self._tasks.pop(0)
        todo()
        if self._tasks:
            t, _ = self._tasks[0]
            delay = max(0, t - time.time())
            self._schedule_next(delay, self._process_next)
        else:
            # Queue is empty, set a watchdog to go off in 60s
            self.insert_task(self.watchdog, 60)
        if not self._scheduled.active():
            raise Exception("NO SCHEDULED TASK!")

    ######################################################################
    # Default implementation of _fire_actors, and _fire_actor
    # Can be used, but should be cleaned up and/or overridden
    ######################################################################

    def _fire_actors(self, actors):
        """
        Try to fire actions on actors on this runtime.
        Parameter 'actors' is a set of actors to try (in that ).
        Returns a set with id of actors that did fire at least one action
        """
        did_fire_actor_ids = set()
        for actor in actors:
            try:
                _log.debug("Fire actor %s (%s, %s)" % (actor.name, actor._type, actor.id))
                # did_fire_action = actor.fire_deprecated()
                did_fire_action = self._fire_actor(actor)
                if did_fire_action:
                    did_fire_actor_ids.add(actor.id)
            except Exception as e:
                _log.exception(e)

        return did_fire_actor_ids

    def _fire_actor(self, actor):
        """
        Try to fire actions on actor on this runtime.
        Returns boolean that is True if actor fired
        """
        #
        # First make sure we are allowed to run
        #
        if not actor._authorized():
            return False

        start_time = time.time()
        actor_did_fire = False
        #
        # Repeatedly go over the action priority list
        #
        done = False
        while not done:
            did_fire, output_ok, exhausted = actor.fire()
            actor_did_fire |= did_fire
            if did_fire:
                #
                # Limit time given to actors even if it could continue a new round of firing
                #
                time_spent = time.time() - start_time
                done = time_spent > 0.020
            else:
                #
                # We reached the end of the list without ANY firing during this round
                # => handle exhaustion and return
                #
                # FIXME: Move exhaust handling to scheduler
                actor._handle_exhaustion(exhausted, output_ok)
                done = True

        return actor_did_fire

    def _fire_actor_non_preemptive(self, actor):
        """
        Try to fire actions on actor on this runtime.
        Returns boolean that is True if actor fired
        """
        #
        # First make sure we are allowed to run
        #
        if not actor._authorized():
            return False

        #
        # Repeatedly go over the action priority list
        #
        done = False
        actor_did_fire = False
        while not done:
            did_fire, output_ok, exhausted = actor.fire()
            actor_did_fire |= did_fire
            if not did_fire:
                #
                # We reached the end of the list without ANY firing during this round
                # => handle exhaustion and return
                #
                # FIXME: Move exhaust handling to scheduler
                actor._handle_exhaustion(exhausted, output_ok)
                done = True

        return actor_did_fire

        
    def _fire_actor_once(self, actor):
        """
        Try to fire action on actor on this runtime.
        Returns boolean that is True if actor fired
        """
        #
        # First make sure we are allowed to run
        #
        if not actor._authorized():
            return False

        did_fire, output_ok, exhausted = actor.fire()
        if not did_fire:
            # => handle exhaustion and return
            #
            # FIXME: Move exhaust handling to scheduler
            actor._handle_exhaustion(exhausted, output_ok)

        return did_fire


######################################################################
# SIMPLE SCHEDULER
######################################################################
class SimpleScheduler(BaseScheduler):

    """A very naive example scheduler deriving from BaseScheduler"""

    def __init__(self, node, actor_mgr):
        super(SimpleScheduler, self).__init__(node, actor_mgr)
        monitor_class = VisualizingMonitor if _log.getEffectiveLevel() is logging.DEBUG else Event_Monitor
        _log.debug("monitor_class is {}".format(monitor_class.__name__))
        self.monitor = monitor_class()

    def tunnel_rx(self, endpoint):
        """Token recieved on endpoint"""
        # We got a token, meaning that the corrsponding actor could possibly fire
        self.insert_task(self.strategy, 0)

    def tunnel_tx_ack(self, endpoint):
        """Token successfully sent on endpoint"""
        # We got back ACK on sent token; at least one slot free in out queue, endpoint can send again at any time
        self.monitor.clear_backoff(endpoint)
        self.insert_task(self.strategy, 0)

    def tunnel_tx_nack(self, endpoint):
        """Token unsuccessfully sent on endpoint"""
        # We got back NACK on sent token, endpoint should wait before resending
        self.monitor.set_backoff(endpoint)
        next_slot = self.monitor.next_slot()
        if next_slot:
            current = time.time()
            self.insert_task(self.strategy, max(0, next_slot - current))

    def tunnel_tx_throttle(self, endpoint):
        """Backoff request for endpoint"""
        # Schedule tx for endpoint at this time
        pass
        # FIXME: Under what circumstances is this method called?

    def schedule_calvinsys(self, actor_id=None):
        """Incoming platform event"""
        self.insert_task(self.strategy, 0)

    def register_endpoint(self, endpoint):
        self.monitor.register_endpoint(endpoint)

    def unregister_endpoint(self, endpoint):
        self.monitor.unregister_endpoint(endpoint)

    # There are at least five things that needs to be done:
    # 1. Call fire() on actors
    # 2. Call communicate on endpoints
    #    2.a Throttle comm if needed
    # 3. Call replication_loop every now and then (handled by base class)
    # 4. Call maintenance_loop every now and then (handled by base class)
    # 5. Implement watchdog as a final resort?

    def strategy(self):
        # Really naive -- always try everything
        list_of_endpoints = self.monitor.endpoints
        did_transfer_tokens = self.monitor.communicate(list_of_endpoints)
        actors_to_fire = self.actor_mgr.enabled_actors()
        did_fire_actor_ids = self._fire_actors(actors_to_fire)
        activity = did_transfer_tokens or bool(did_fire_actor_ids)
        if activity:
            self.insert_task(self.strategy, 0)

    def watchdog(self):
        # Log and try to get back on track....
        _log.warning("WATCHDOG TRIGGERED")
        self.insert_task(self.strategy, 0)

######################################################################
# ROUND-ROBIN SCHEDULER
######################################################################
class RoundRobinScheduler(SimpleScheduler):
    
    def strategy(self):
        # Communicate
        list_of_endpoints = self.monitor.endpoints
        did_transfer_tokens = self.monitor.communicate(list_of_endpoints)
        # Round Robin
        actors_to_fire = self.actor_mgr.enabled_actors()
        did_fire_actor_ids = [actor.id for actor in actors_to_fire if self._fire_actor_once(actor)]
        # Repeat if there was any activity
        activity = did_transfer_tokens or bool(did_fire_actor_ids)
        if activity:
            self.insert_task(self.strategy, 0)


######################################################################
# NON-PREEMPTIVE SCHEDULER
######################################################################
class NonPreemptiveScheduler(SimpleScheduler):
    
    def strategy(self):
        # Communicate
        list_of_endpoints = self.monitor.endpoints
        did_transfer_tokens = self.monitor.communicate(list_of_endpoints)
        # Non-preemptive
        actors_to_fire = self.actor_mgr.enabled_actors()
        did_fire_actor_ids = [actor.id for actor in actors_to_fire if self._fire_actor_non_preemptive(actor)]
        # Repeat if there was any activity
        activity = did_transfer_tokens or bool(did_fire_actor_ids)
        if activity:
            self.insert_task(self.strategy, 0)

######################################################################
# DEPRECATED SCHEDULER
######################################################################
class BaselineScheduler(object):
    
    """This is the old scheduler that should go away..."""

    def __init__(self, node, actor_mgr):
        super(BaselineScheduler, self).__init__()
        self.actor_mgr = actor_mgr
        self.done = False
        self.node = node
        self.monitor = Event_Monitor()
        self._scheduled = None
        self._trigger_set = set()
        self._watchdog = None
        self._watchdog_timeout = 1
        self._maintenance_loop = None
        self._maintenance_delay = _conf.get(None, "maintenance_delay") or 300

    def run(self):
        async.run_ioloop()

    def stop(self):
        if not self.done:
            async.DelayedCall(0, async.stop_ioloop)
        self.done = True

    #
    # Helper methods
    #
    def all_actors(self):
        return self.actor_mgr.enabled_actors()

    def all_actor_ids(self):
        return [a.id for a in self.all_actors()]

    def pending_actors(self):
        actors = self.all_actors()
        pending = [a for a in actors if a.id in self._trigger_set]
        return pending

    def pending(self):
        return self._trigger_set

    def clear_pending(self):
        self._trigger_set = set()

    def update_pending(self, actor_ids):
        self._trigger_set.update(actor_ids)

    #
    # Event "API" used by subsystems to inform scheduler about events
    #
    def tunnel_rx(self, endpoint):
        """Token recieved on endpoint"""
        # _log.warning("schedule::tunnel_rx")
        # We got a token, meaning that the corrsponding actor could possibly fire
        self._schedule_actors(actor_ids=[endpoint.port.owner.id])

    def tunnel_tx_ack(self, endpoint):
        """Token successfully sent on endpoint"""
        # _log.warning("schedule::tunnel_tx_ack")
        # We got back ACK on sent token; at least one slot free in out queue, endpoint can send again at any time
        self.monitor.clear_backoff(endpoint)
        self._schedule_all()

    def tunnel_tx_nack(self, endpoint):
        """Token unsuccessfully sent on endpoint"""
        # _log.warning("schedule::tunnel_tx_nack")
        # We got back NACK on sent token, endpoint should wait before resending
        self.monitor.set_backoff(endpoint)

    def tunnel_tx_throttle(self, endpoint):
        """Backoff request for endpoint"""
        # _log.warning("schedule::tx_throttle")
        # FIXME: schedule at this time if nothing else to be done (presently done in strategy)
        pass

    def schedule_calvinsys(self, actor_id=None):
        """Incoming platform event"""
        # FIXME: When old calvinsys is gone, actor_id will always be present
        if actor_id is None:
            self._schedule_all()
        else:
            self._schedule_actors(actor_ids=[actor_id])

    def register_endpoint(self, endpoint):
        self.monitor.register_endpoint(endpoint)

    def unregister_endpoint(self, endpoint):
        self.monitor.unregister_endpoint(endpoint)
    #
    # Capture the scheduling logic here?
    #
    def strategy(self, did_transfer_tokens, did_fire_actor_ids):
        # If the did_fire_actor_ids set is empty no actor could fire any action
        did_fire = bool(did_fire_actor_ids)
        activity = did_fire or did_transfer_tokens
        # print "STRATEGY:", did_transfer_tokens, did_fire_actor_ids, did_fire, activity
        if activity:
            # Something happened - run again
            # We would like to call _schedule_actors with a list of actors, like so ...
            # self._schedule_actors(actor_ids=actor_ids)
            # ... but we don't have a strategy, so run'em all :(
            self._schedule_all()
            # print "STRATEGY: _schedule_all"
        else:
            next_slot = self.monitor.next_slot()
            if next_slot:
                current = time.time()
                self._schedule_all(max(0, next_slot - current))
            else:
                # No firings, set a watchdog timeout
                self._schedule_watchdog()
                # print "STRATEGY: _schedule_watchdog"

    #
    # Maintenance loop
    #
    # FIXME: Deal with this later
    def maintenance_loop(self):
        # Migrate denied actors
        for actor in self.actor_mgr.migratable_actors():
            self.actor_mgr.migrate(actor.id, actor.migration_info["node_id"],
                                   callback=CalvinCB(actor.remove_migration_info))
        # Enable denied actors again if access is permitted. Will try to migrate if access still denied.
        for actor in self.actor_mgr.denied_actors():
            actor.enable_or_migrate()
        # TODO: try to migrate shadow actors as well.
        self._maintenance_loop = None
        self.trigger_maintenance_loop(delay=True)

    def trigger_maintenance_loop(self, delay=False):
        # Never have more then one maintenance loop.
        if self._maintenance_loop is not None:
            self._maintenance_loop.cancel()
        if delay:
            self._maintenance_loop = async.DelayedCall(self._maintenance_delay, self.maintenance_loop)
        else:
            self._maintenance_loop = async.DelayedCall(0, self.maintenance_loop)

    #
    # Private methods
    #
    # FIXME: Clean up, and split in common and specific functionality so we can use this as
    #        a base class for schedulers
    def _loop_once(self):
        # We don't know how we got here, so cancel both of these (safe thing to do)
        self._cancel_watchdog()
        self._cancel_schedule()
        # Transfer tokens between actors
        # Until we track this, supply all endpoints to communicate
        list_of_endpoints = self.monitor.endpoints
        did_transfer_tokens = self.monitor.communicate(list_of_endpoints)
        # Pick which set of actors to fire
        actors_to_fire = self.pending_actors()
        # Reset the set of potential actors
        self.clear_pending()
        did_fire_actor_ids = self._fire_actors(actors_to_fire)
        # Control replication
        self.node.rm.replication_loop()
        # Figure out strategy
        self.strategy(did_transfer_tokens, did_fire_actor_ids)



    def _fire_actors(self, actors):
        """
        Try to fire actions on actors on this runtime.
        Parameter 'actors' is a set of actors to try (in that ).
        Returns a set with id of actors that did fire at least one action
        """
        did_fire_actor_ids = set()
        for actor in actors:
            try:
                _log.debug("Fire actor %s (%s, %s)" % (actor.name, actor._type, actor.id))
                # did_fire_action = actor.fire_deprecated()
                did_fire_action = self._fire_actor(actor)
                if did_fire_action:
                    did_fire_actor_ids.add(actor.id)
            except Exception as e:
                self._log_exception_during_fire(e)

        return did_fire_actor_ids

    def _fire_actor(self, actor):
        """
        Try to fire actions on actor on this runtime.
        Returns boolean that is True if actor fired
        """
        #
        # First make sure we are allowed to run
        #
        if not actor._authorized():
            return False

        start_time = time.time()
        actor_did_fire = False
        #
        # Repeatedly go over the action priority list
        #
        done = False
        while not done:
            did_fire, output_ok, exhausted = actor.fire()
            actor_did_fire |= did_fire
            if did_fire:
                #
                # Limit time given to actors even if it could continue a new round of firing
                #
                time_spent = time.time() - start_time
                done = time_spent > 0.020
            else:
                #
                # We reached the end of the list without ANY firing during this round
                # => handle exhaustion and return
                #
                # FIXME: Move exhaust handling to scheduler
                actor._handle_exhaustion(exhausted, output_ok)
                done = True

        return actor_did_fire

    def _cancel_schedule(self):
        if self._scheduled is not None:
            self._scheduled.cancel()
            self._scheduled = None

    def _cancel_watchdog(self):
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    def _watchdog_wakeup(self):
        _log.warning("Watchdog wakeup -- this should not really happen...")
        self._schedule_all()

    def _schedule_watchdog(self):
        self._watchdog = async.DelayedCall(self._watchdog_timeout, self._watchdog_wakeup)

    def _schedule_all(self, delay=0):
        # If there is a scheduled _loop_once then cancel it since it might be
        # scheduled later, and/or with argument set to False.
        self._cancel_watchdog()
        self._cancel_schedule()
        self.update_pending(self.all_actor_ids())
        # print "Pending: ", self.all_actor_ids(), self.pending()
        self._scheduled = async.DelayedCall(delay, self._loop_once)

    def _schedule_actors(self, actor_ids):
        # Update the set of actors that could possibly fire
        self.update_pending(actor_ids)
        # Schedule _loop_once if-and-only-if
        #                    it is not already scheduled
        #                    AND
        #                    the set of actors is non-empty
        if self._scheduled is None:
            if self.pending():
                self._cancel_watchdog()
                self._scheduled = async.DelayedCall(0, self._loop_once)


    def _log_exception_during_fire(self, e):
        _log.exception(e)


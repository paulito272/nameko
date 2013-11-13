from __future__ import absolute_import

from abc import ABCMeta, abstractproperty
from logging import getLogger

import eventlet
from eventlet.event import Event
from eventlet.greenpool import GreenPool
import greenlet

from nameko.dependencies import get_dependencies, DependencySet
from nameko.exceptions import RemoteError
from nameko.logging import log_time
from nameko.utils import SpawningProxy

_log = getLogger(__name__)


MAX_WOKERS_KEY = 'max_workers'
KILL_TIMEOUT = 3  # seconds

NAMEKO_DATA_KEYS = (
    'language',
    'user_id',
    'auth_token',
)


def get_service_name(service_cls):
    return getattr(service_cls, "name", service_cls.__name__.lower())


def log_worker_exception(worker_ctx, exc):
    if isinstance(exc, RemoteError):
        exc = "RemoteError"
    _log.error('error handling worker %s: %s', worker_ctx, exc, exc_info=True)


class WorkerContextBase(object):
    """ Abstract base class for a WorkerContext
    """
    __metaclass__ = ABCMeta

    def __init__(self, container, service, method_name, args=None, kwargs=None,
                 data=None):
        self.container = container
        self.config = container.config  # TODO: remove?
        self.service = service
        self.method_name = method_name
        self.args = args if args is not None else ()
        self.kwargs = kwargs if kwargs is not None else {}
        self.data = data if data is not None else {}

    @abstractproperty
    def data_keys(self):
        """ Return a tuple of keys describing data kept on this WorkerContext.
        """

    def __str__(self):
        cls_name = type(self).__name__
        service_name = self.container.service_name
        return '<{} {}.{} at 0x{:x}>'.format(
            cls_name, service_name, self.method_name, id(self))


class WorkerContext(WorkerContextBase):
    """ Default WorkerContext implementation
    """
    data_keys = NAMEKO_DATA_KEYS


class ManagedThreadContainer(object):
    def __init__(self):
        self._active_threads = set()
        self._died = Event()

    def stop(self):
        """ Stop the container gracefully.

        First all entrypoints are asked to ``stop()``.
        This ensures that no new worker threads are started.

        It is the providers' responsiblity to gracefully shut down when
        ``stop()`` is called on them and only return when they have stopped.

        After all entrypoints have stopped the container waits for any
        active workers to complete.

        After all active workers have stopped the container stops all
        injections.

        At this point there should be no more active threads. In case there
        are any active threads, they are killed by the container.
        """
        if self._died.ready():
            _log.debug('already stopped %s', self)
            return

        _log.debug('stopping %s', self)

        with log_time(_log.debug, 'stopped %s in %0.3f sec', self):
            self._handle_container_stop()

            # just in case there was a provider not taking care of it's worker
            self._kill_active_threads()

            self._died.send(None)

    def _handle_container_stop(self):
        return

    def kill(self, exc):
        """ Kill the container in a semi-graceful way.

        First all dependencies have a chance to kill themselves
        within a given time limit (``KILL_TIMEOUT``).

        To do so they must implement a ``kill(exc)`` method and take care
        of shutting down any resources when that method is called on them.

        After all the dependencies have been killed or ``KILL_TIMEOUT``
        has been reached, all remaining active threads, worker and
        managed ones, are explicitly killed by the container.

        The container dies with the given ``exc``.
        """
        if self._died.ready():
            _log.debug('already stopped %s', self)
            return

        self._handle_container_kill(exc)

        _log.info('killing container due to "%s"', exc)

        self._kill_active_threads()
        self._died.send_exception(exc)

    def _handle_container_kill(self, exc):
        return

    def _kill_active_threads(self):
        num_active_threads = len(self._active_threads)

        if num_active_threads:
            _log.warning('killing active threads (%s)', num_active_threads)
            for gt in list(self._active_threads):
                gt.kill()

    def wait(self):
        """ Block until the container has been stopped.

        If the container was stopped using ``kill(exc)``,
        ``wait()`` raises ``exc``.
        Any unhandled exception raised in a managed thread or in the
        life-cycle management code also causes the container to be
        ``kill()``ed, which causes an exception to be raised from ``wait()``.
        """
        return self._died.wait()

    def spawn_managed_thread(self, run_method):
        """ Spawn a lifecycle-managed thread, which calls the ``run_method``
        once it has been started.

        Any errors raised inside the ``run_method`` cause the container to be
        killed.

        It is the entrypoint provider's responsibility to react to ``stop()``
        calls and terminate it's spawned threads.

        Threads are killed automatically by the container if they are
        still running after all their providers have been stopped.

        Entrypoints may only create separate threads using this method,
        to ensure they are life-cycle managed.
        """
        gt = eventlet.spawn(run_method)
        self._active_threads.add(gt)
        gt.link(self._handle_thread_exited)
        return gt

    def _handle_thread_exited(self, gt):
        self._active_threads.remove(gt)

        try:
            gt.wait()

        except greenlet.GreenletExit:
            # we don't care much about threads killed by the container
            # this can happen in stop() and kill() if providers
            # don't properly take care of their threads
            _log.warning('%s thread killed by container', self)

        except Exception as exc:
            _log.error('%s thread exited with error', self,
                       exc_info=True)
            # any error raised inside an active thread is unexpected behavior
            # and probably a bug in the providers or container
            # to be safe we kill the container
            self.kill(exc)


class ServiceContainer(ManagedThreadContainer):

    def __init__(self, service_cls, worker_ctx_cls, config):
        super(ServiceContainer, self).__init__()
        self.service_cls = service_cls
        self.worker_ctx_cls = worker_ctx_cls

        self.service_name = get_service_name(service_cls)

        self.config = config
        self.max_workers = config.get(MAX_WOKERS_KEY, 10) or 10

        self.dependencies = DependencySet()
        for dep in get_dependencies(self):
            self.dependencies.add(dep)

        self._worker_pool = GreenPool(size=self.max_workers)

    def start(self):
        """ Start a container by starting all the dependency providers.
        """
        _log.debug('starting %s', self)

        with log_time(_log.debug, 'started %s in %0.3f sec', self):
            self.dependencies.all.prepare()
            self.dependencies.all.start()

    def _handle_container_stop(self):
        dependencies = self.dependencies

        # entrypoint deps have to be stopped before injection deps
        # to ensure that running workers can successfully complete
        dependencies.entrypoints.all.stop()

        # there might still be some running workers, which we have to
        # wait for to complete before we can stop injection dependencies
        self._worker_pool.waitall()

        # it should be safe now to stop any injection as ther is no
        # active worker which could be using it
        dependencies.injections.all.stop()

    def _handle_container_kill(self, exc):
        try:
            with eventlet.Timeout(KILL_TIMEOUT):
                self.dependencies.all.kill(exc)
        except eventlet.Timeout:
            _log.warning('timeout waiting for dependencies.kill %s', self)

    def spawn_worker(self, provider, args, kwargs,
                     context_data=None, handle_result=None):
        """ Spawn a worker thread for running the service method decorated
        with an entrypoint ``provider``.

        ``args`` and ``kwargs`` are used as arguments for the service
        method.

        ``context_data`` is used to initialize a ``WorkerContext``.

        ``handle_result`` is an optional callback which may be passed
        in by the calling entrypoint provider. It is called with the
        result returned or error raised by the service method.
        """

        service = self.service_cls()
        worker_ctx = self.worker_ctx_cls(
            self, service, provider.name, args, kwargs, data=context_data)

        _log.debug('spawning %s', worker_ctx)
        gt = self._worker_pool.spawn(self._run_worker, worker_ctx,
                                     handle_result)
        self._active_threads.add(gt)
        gt.link(self._handle_thread_exited)
        return worker_ctx

    def _run_worker(self, worker_ctx, handle_result):
        _log.debug('setting up %s', worker_ctx)

        with log_time(_log.debug, 'ran worker %s in %0.3fsec', worker_ctx):

            self.dependencies.injections.all.inject(worker_ctx)
            self.dependencies.all.worker_setup(worker_ctx)

            result = exc = None
            try:
                _log.debug('calling handler for %s', worker_ctx)

                method = getattr(worker_ctx.service, worker_ctx.method_name)

                with log_time(_log.debug, 'ran handler for %s in %0.3fsec',
                              worker_ctx):
                    result = method(*worker_ctx.args, **worker_ctx.kwargs)
            except Exception as e:
                log_worker_exception(worker_ctx, e)
                exc = e

            if handle_result is not None:
                _log.debug('handling result for %s', worker_ctx)

                with log_time(_log.debug, 'handled result for %s in %0.3fsec',
                              worker_ctx):
                    handle_result(worker_ctx, result, exc)

            with log_time(_log.debug, 'tore down worker %s in %0.3fsec',
                          worker_ctx):

                _log.debug('signalling result for %s', worker_ctx)
                self.dependencies.injections.all.worker_result(
                    worker_ctx, result, exc)

                _log.debug('tearing down %s', worker_ctx)
                self.dependencies.all.worker_teardown(worker_ctx)
                self.dependencies.injections.all.release(worker_ctx)

    def __str__(self):
        return '<ServiceContainer {} at 0x{:x}>'.format(
            self.service_name, id(self))


class ServiceRunner(object):
    """ Allows the user to serve a number of services concurrently.
    The caller can register a number of service classes with a name and
    then use the start method to serve them and the stop and kill methods
    to stop them. The wait method will block until all services have stopped.

    Example:

        runner = ServiceRunner(config)
        runner.add_service('foobar', Foobar)
        runner.add_service('spam', Spam)

        add_sig_term_handler(runner.kill)

        runner.start()

        runner.wait()
    """
    def __init__(self, config, container_cls=ServiceContainer):
        self.service_map = {}
        self.containers = []
        self.config = config
        self.container_cls = container_cls

    def add_service(self, cls, worker_ctx_cls=WorkerContext):
        """ Adds a service class to the runner.
        There can only be one service class for a given service name.
        Service classes must be registered before calling start()
        """
        service_name = get_service_name(cls)
        self.service_map[service_name] = (cls, worker_ctx_cls)

    def start(self):
        """ Starts all the registered services.

        A new container will be created for each service using the container
        class provided in the __init__ method.

        All containers will be started concurently and the method will block
        until all have completed their startup routine.
        """
        config = self.config
        service_map = self.service_map
        _log.info('starting services: %s', service_map.keys())

        for _, (service_cls, worker_ctx_cls) in service_map.items():
            container = self.container_cls(service_cls, worker_ctx_cls, config)
            self.containers.append(container)

        SpawningProxy(self.containers).start()

        _log.info('services started: %s', service_map.keys())

    def stop(self):
        """ Stops all running containers concurrently.
        The method will block until all containers have stopped.
        """
        service_map = self.service_map
        _log.info('stopping services: %s', service_map.keys())

        SpawningProxy(self.containers).stop()

        _log.info('services stopped: %s', service_map.keys())

    def kill(self):
        """ Kill all running containers concurrently.
        The method will block until all containers have stopped.
        """

        service_map = self.service_map
        _log.info('killing services: %s', service_map.keys())

        SpawningProxy(self.containers).kill()

        _log.info('services killed: %s ', service_map.keys())

    def wait(self):
        """ Waits for all running containers to stop.
        """
        SpawningProxy(self.containers).wait()

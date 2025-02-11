# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
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

"""Quotas for shares."""

import datetime

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import importutils
from oslo_utils import timeutils
import six

from karbor import db
from karbor import exception
from karbor.i18n import _

LOG = logging.getLogger(__name__)

quota_opts = [
    cfg.IntOpt('quota_plans',
               default=50,
               help='The number of volume backups allowed per project'),
    cfg.IntOpt('quota_checkpoints',
               default=-1,
               help='The number of checkpoints allowed per project'),
    cfg.IntOpt('reservation_expire',
               default=86400,
               help='number of seconds until a reservation expires'),
    cfg.IntOpt('until_refresh',
               default=0,
               help='count of reservations until usage is refreshed'),
    cfg.IntOpt('max_age',
               default=0,
               help='number of seconds between subsequent usage refreshes'),
    cfg.StrOpt('quota_driver',
               default='karbor.quota.DbQuotaDriver',
               help='default driver to use for quota checks'), ]

CONF = cfg.CONF
CONF.register_opts(quota_opts)


class DbQuotaDriver(object):
    """Driver to perform necessary checks to enforce quotas and obtain

    quota information.  The default driver utilizes the local
    database.
    """

    def get_by_project(self, context, project_id, resource):
        """Get a specific quota by project."""

        return db.quota_get(context, project_id, resource)

    def get_by_class(self, context, quota_class, resource):
        """Get a specific quota by quota class."""

        return db.quota_class_get(context, quota_class, resource)

    def get_defaults(self, context, resources):
        """Given a list of resources, retrieve the default quotas.

        :param context: The request context, for access checks.
        :param resources: A dictionary of the registered resources.
        """

        quotas = {}
        for resource in resources.values():
            quotas[resource.name] = resource.default

        return quotas

    def get_class_quotas(self, context, resources, quota_class,
                         defaults=True):
        """Given a list of resources, retrieve the quotas for the given quota class.

        :param context: The request context, for access checks.
        :param resources: A dictionary of the registered resources.
        :param quota_class: The name of the quota class to return
                            quotas for.
        :param defaults: If True, the default value will be reported
                         if there is no specific value for the
                         resource.
        """

        quotas = {}
        class_quotas = db.quota_class_get_all_by_name(context, quota_class)
        for resource in resources.values():
            if defaults or resource.name in class_quotas:
                quotas[resource.name] = class_quotas.get(resource.name,
                                                         resource.default)

        return quotas

    def get_project_quotas(self, context, resources, project_id,
                           quota_class=None, defaults=True,
                           usages=True):
        """Given a list of resources, retrieve the quotas for the given project.

        :param context: The request context, for access checks.
        :param resources: A dictionary of the registered resources.
        :param project_id: The ID of the project to return quotas for.
        :param quota_class: If project_id != context.project_id, the
                            quota class cannot be determined.  This
                            parameter allows it to be specified.  It
                            will be ignored if project_id ==
                            context.project_id.
        :param defaults: If True, the quota class value (or the
                         default value, if there is no value from the
                         quota class) will be reported if there is no
                         specific value for the resource.
        :param usages: If True, the current in_use and reserved counts
                       will also be returned.
        """

        quotas = {}
        project_quotas = db.quota_get_all_by_project(context, project_id)
        if usages:
            project_usages = db.quota_usage_get_all_by_project(context,
                                                               project_id)

        # Get the quotas for the appropriate class.  If the project ID
        # matches the one in the context, we use the quota_class from
        # the context, otherwise, we use the provided quota_class (if
        # any)
        if project_id == context.project_id:
            quota_class = context.quota_class
        if quota_class:
            class_quotas = db.quota_class_get_all_by_name(context, quota_class)
        else:
            class_quotas = {}

        for resource in resources.values():
            # Omit default/quota class values
            if not defaults and resource.name not in project_quotas:
                continue

            quotas[resource.name] = dict(
                limit=project_quotas.get(resource.name,
                                         class_quotas.get(resource.name,
                                                          resource.default)), )

            # Include usages if desired.  This is optional because one
            # internal consumer of this interface wants to access the
            # usages directly from inside a transaction.
            if usages:
                usage = project_usages.get(resource.name, {})
                quotas[resource.name].update(
                    in_use=usage.get('in_use', 0),
                    reserved=usage.get('reserved', 0), )

        return quotas

    def _get_quotas(self, context, resources, keys, has_sync, project_id=None):
        """A helper method which retrieves the quotas for the specific

        resources identified by keys, and which apply to the current
        context.

        :param context: The request context, for access checks.
        :param resources: A dictionary of the registered resources.
        :param keys: A list of the desired quotas to retrieve.
        :param has_sync: If True, indicates that the resource must
                         have a sync attribute; if False, indicates
                         that the resource must NOT have a sync
                         attribute.
        :param project_id: Specify the project_id if current context
                           is admin and admin wants to impact on
                           common user's tenant.
        """

        # Filter resources
        if has_sync:
            sync_filt = lambda x: hasattr(x, 'sync')
        else:
            sync_filt = lambda x: not hasattr(x, 'sync')
        desired = set(keys)
        sub_resources = dict((k, v) for k, v in resources.items()
                             if k in desired and sync_filt(v))

        # Make sure we accounted for all of them...
        if len(keys) != len(sub_resources):
            unknown = desired - set(sub_resources.keys())
            raise exception.QuotaResourceUnknown(unknown=sorted(unknown))

        # Grab and return the quotas (without usages)
        quotas = self.get_project_quotas(context, sub_resources,
                                         project_id,
                                         context.quota_class, usages=False)

        return dict((k, v['limit']) for k, v in quotas.items())

    def limit_check(self, context, resources, values, project_id=None):
        """Check simple quota limits.

        For limits--those quotas for which there is no usage
        synchronization function--this method checks that a set of
        proposed values are permitted by the limit restriction.

        This method will raise a QuotaResourceUnknown exception if a
        given resource is unknown or if it is not a simple limit
        resource.

        If any of the proposed values is over the defined quota, an
        OverQuota exception will be raised with the sorted list of the
        resources which are too high.  Otherwise, the method returns
        nothing.

        :param context: The request context, for access checks.
        :param resources: A dictionary of the registered resources.
        :param values: A dictionary of the values to check against the
                       quota.
        :param project_id: Specify the project_id if current context
                           is admin and admin wants to impact on
                           common user's tenant.
        """

        # Ensure no value is less than zero
        unders = [key for key, val in values.items() if val < 0]
        if unders:
            raise exception.InvalidQuotaValue(unders=sorted(unders))

        # If project_id is None, then we use the project_id in context
        if project_id is None:
            project_id = context.project_id

        # Get the applicable quotas
        quotas = self._get_quotas(context, resources, values.keys(),
                                  has_sync=False, project_id=project_id)
        # Check the quotas and construct a list of the resources that
        # would be put over limit by the desired values
        overs = [key for key, val in values.items()
                 if quotas[key] >= 0 and quotas[key] < val]
        if overs:
            raise exception.OverQuota(overs=sorted(overs), quotas=quotas,
                                      usages={})

    def reserve(self, context, resources, deltas, expire=None,
                project_id=None):
        """Check quotas and reserve resources.

        For counting quotas--those quotas for which there is a usage
        synchronization function--this method checks quotas against
        current usage and the desired deltas.

        This method will raise a QuotaResourceUnknown exception if a
        given resource is unknown or if it does not have a usage
        synchronization function.

        If any of the proposed values is over the defined quota, an
        OverQuota exception will be raised with the sorted list of the
        resources which are too high.  Otherwise, the method returns a
        list of reservation UUIDs which were created.

        :param context: The request context, for access checks.
        :param resources: A dictionary of the registered resources.
        :param deltas: A dictionary of the proposed delta changes.
        :param expire: An optional parameter specifying an expiration
                       time for the reservations.  If it is a simple
                       number, it is interpreted as a number of
                       seconds and added to the current time; if it is
                       a datetime.timedelta object, it will also be
                       added to the current time.  A datetime.datetime
                       object will be interpreted as the absolute
                       expiration time.  If None is specified, the
                       default expiration time set by
                       --default-reservation-expire will be used (this
                       value will be treated as a number of seconds).
        :param project_id: Specify the project_id if current context
                           is admin and admin wants to impact on
                           common user's tenant.
        """

        # Set up the reservation expiration
        if expire is None:
            expire = CONF.reservation_expire
        if isinstance(expire, six.integer_types):
            expire = datetime.timedelta(seconds=expire)
        if isinstance(expire, datetime.timedelta):
            expire = timeutils.utcnow() + expire
        if not isinstance(expire, datetime.datetime):
            raise exception.InvalidReservationExpiration(expire=expire)

        # If project_id is None, then we use the project_id in context
        if project_id is None:
            project_id = context.project_id

        # Get the applicable quotas.
        # NOTE(Vek): We're not worried about races at this point.
        #            Yes, the admin may be in the process of reducing
        #            quotas, but that's a pretty rare thing.
        quotas = self._get_quotas(context, resources, deltas.keys(),
                                  has_sync=True, project_id=project_id)

        # NOTE(Vek): Most of the work here has to be done in the DB
        #            API, because we have to do it in a transaction,
        #            which means access to the session.  Since the
        #            session isn't available outside the DBAPI, we
        #            have to do the work there.
        return db.quota_reserve(context, resources, quotas, deltas, expire,
                                CONF.until_refresh, CONF.max_age,
                                project_id=project_id)

    def commit(self, context, reservations, project_id=None):
        """Commit reservations.

        :param context: The request context, for access checks.
        :param reservations: A list of the reservation UUIDs, as
                             returned by the reserve() method.
        :param project_id: Specify the project_id if current context
                           is admin and admin wants to impact on
                           common user's tenant.
        """
        # If project_id is None, then we use the project_id in context
        if project_id is None:
            project_id = context.project_id

        db.reservation_commit(context, reservations, project_id=project_id)

    def rollback(self, context, reservations, project_id=None):
        """Roll back reservations.

        :param context: The request context, for access checks.
        :param reservations: A list of the reservation UUIDs, as
                             returned by the reserve() method.
        :param project_id: Specify the project_id if current context
                           is admin and admin wants to impact on
                           common user's tenant.
        """
        # If project_id is None, then we use the project_id in context
        if project_id is None:
            project_id = context.project_id

        db.reservation_rollback(context, reservations, project_id=project_id)

    def destroy_all_by_project(self, context, project_id):
        """Destroy all quotas, usages, and reservations associated with a project.

        :param context: The request context, for access checks.
        :param project_id: The ID of the project being deleted.
        """

        db.quota_destroy_all_by_project(context, project_id)

    def expire(self, context):
        """Expire reservations.

        Explores all currently existing reservations and rolls back
        any that have expired.

        :param context: The request context, for access checks.
        """

        db.reservation_expire(context)


class BaseResource(object):
    """Describe a single resource for quota checking."""

    def __init__(self, name, flag=None):
        """Initializes a Resource.

        :param name: The name of the resource, i.e., "shares".
        :param flag: The name of the flag or configuration option
                     which specifies the default value of the quota
                     for this resource.
        """

        self.name = name
        self.flag = flag

    def quota(self, driver, context, **kwargs):
        """Given a driver and context, obtain the quota for this resource.

        :param driver: A quota driver.
        :param context: The request context.
        :param project_id: The project to obtain the quota value for.
                           If not provided, it is taken from the
                           context.  If it is given as None, no
                           project-specific quota will be searched
                           for.
        :param quota_class: The quota class corresponding to the
                            project, or for which the quota is to be
                            looked up.  If not provided, it is taken
                            from the context.  If it is given as None,
                            no quota class-specific quota will be
                            searched for.  Note that the quota class
                            defaults to the value in the context,
                            which may not correspond to the project if
                            project_id is not the same as the one in
                            the context.
        """

        # Get the project ID
        project_id = kwargs.get('project_id', context.project_id)

        # Ditto for the quota class
        quota_class = kwargs.get('quota_class', context.quota_class)

        # Look up the quota for the project
        if project_id:
            try:
                return driver.get_by_project(context, project_id, self.name)
            except exception.ProjectQuotaNotFound:
                pass

        # Try for the quota class
        if quota_class:
            try:
                return driver.get_by_class(context, quota_class, self.name)
            except exception.QuotaClassNotFound:
                pass

        # OK, return the default
        return self.default

    @property
    def default(self):
        """Return the default value of the quota."""

        return CONF[self.flag] if self.flag else -1


class ReservableResource(BaseResource):
    """Describe a reservable resource."""

    def __init__(self, name, sync, flag=None):
        """Initializes a ReservableResource.

        Reservable resources are those resources which directly
        correspond to objects in the database, i.e., shares, gigabytes,
        etc.  A ReservableResource must be constructed with a usage
        synchronization function, which will be called to determine the
        current counts of one or more resources.

        The usage synchronization function will be passed three
        arguments: an admin context, the project ID, and an opaque
        session object, which should in turn be passed to the
        underlying database function.  Synchronization functions
        should return a dictionary mapping resource names to the
        current in_use count for those resources; more than one
        resource and resource count may be returned.  Note that
        synchronization functions may be associated with more than one
        ReservableResource.

        :param name: The name of the resource, i.e., "shares".
        :param sync: A callable which returns a dictionary to
                     resynchronize the in_use count for one or more
                     resources, as described above.
        :param flag: The name of the flag or configuration option
                     which specifies the default value of the quota
                     for this resource.
        """

        super(ReservableResource, self).__init__(name, flag=flag)
        self.sync = sync


class AbsoluteResource(BaseResource):
    """Describe a non-reservable resource."""

    pass


class CountableResource(AbsoluteResource):
    """Describe a resource where the counts aren't based solely on the

    project ID.
    """

    def __init__(self, name, count, flag=None):
        """Initializes a CountableResource.

        Countable resources are those resources which directly
        correspond to objects in the database, i.e., shares, gigabytes,
        etc., but for which a count by project ID is inappropriate.  A
        CountableResource must be constructed with a counting
        function, which will be called to determine the current counts
        of the resource.

        The counting function will be passed the context, along with
        the extra positional and keyword arguments that are passed to
        Quota.count().  It should return an integer specifying the
        count.

        Note that this counting is not performed in a transaction-safe
        manner.  This resource class is a temporary measure to provide
        required functionality, until a better approach to solving
        this problem can be evolved.

        :param name: The name of the resource, i.e., "shares".
        :param count: A callable which returns the count of the
                      resource.  The arguments passed are as described
                      above.
        :param flag: The name of the flag or configuration option
                     which specifies the default value of the quota
                     for this resource.
        """

        super(CountableResource, self).__init__(name, flag=flag)
        self.count = count


class QuotaEngine(object):
    """Represent the set of recognized quotas."""

    def __init__(self, quota_driver_class=None):
        """Initialize a Quota object."""

        if not quota_driver_class:
            quota_driver_class = CONF.quota_driver

        if isinstance(quota_driver_class, six.string_types):
            quota_driver_class = importutils.import_object(quota_driver_class)

        self._resources = {}
        self._driver = quota_driver_class

    def __contains__(self, resource):
        return resource in self._resources

    def register_resource(self, resource):
        """Register a resource."""

        self._resources[resource.name] = resource

    def register_resources(self, resources):
        """Register a list of resources."""

        for resource in resources:
            self.register_resource(resource)

    def get_by_project(self, context, project_id, resource):
        """Get a specific quota by project."""

        return self._driver.get_by_project(context, project_id, resource)

    def get_by_class(self, context, quota_class, resource):
        """Get a specific quota by quota class."""

        return self._driver.get_by_class(context, quota_class, resource)

    def get_defaults(self, context):
        """Retrieve the default quotas.

        :param context: The request context, for access checks.
        """

        return self._driver.get_defaults(context, self._resources)

    def get_class_quotas(self, context, quota_class, defaults=True):
        """Retrieve the quotas for the given quota class.

        :param context: The request context, for access checks.
        :param quota_class: The name of the quota class to return
                            quotas for.
        :param defaults: If True, the default value will be reported
                         if there is no specific value for the
                         resource.
        """

        return self._driver.get_class_quotas(context, self._resources,
                                             quota_class, defaults=defaults)

    def get_project_quotas(self, context, project_id, quota_class=None,
                           defaults=True, usages=True):
        """Retrieve the quotas for the given project.

        :param context: The request context, for access checks.
        :param project_id: The ID of the project to return quotas for.
        :param quota_class: If project_id != context.project_id, the
                            quota class cannot be determined.  This
                            parameter allows it to be specified.
        :param defaults: If True, the quota class value (or the
                         default value, if there is no value from the
                         quota class) will be reported if there is no
                         specific value for the resource.
        :param usages: If True, the current in_use and reserved counts
                       will also be returned.
        """

        return self._driver.get_project_quotas(context, self._resources,
                                               project_id,
                                               quota_class=quota_class,
                                               defaults=defaults,
                                               usages=usages)

    def count(self, context, resource, *args, **kwargs):
        """Count a resource.

        For countable resources, invokes the count() function and
        returns its result.  Arguments following the context and
        resource are passed directly to the count function declared by
        the resource.

        :param context: The request context, for access checks.
        :param resource: The name of the resource, as a string.
        """

        # Get the resource
        res = self._resources.get(resource)
        if not res or not hasattr(res, 'count'):
            raise exception.QuotaResourceUnknown(unknown=[resource])

        return res.count(context, *args, **kwargs)

    def limit_check(self, context, project_id=None, **values):
        """Check simple quota limits.

        For limits--those quotas for which there is no usage
        synchronization function--this method checks that a set of
        proposed values are permitted by the limit restriction.  The
        values to check are given as keyword arguments, where the key
        identifies the specific quota limit to check, and the value is
        the proposed value.

        This method will raise a QuotaResourceUnknown exception if a
        given resource is unknown or if it is not a simple limit
        resource.

        If any of the proposed values is over the defined quota, an
        OverQuota exception will be raised with the sorted list of the
        resources which are too high.  Otherwise, the method returns
        nothing.

        :param context: The request context, for access checks.
        :param project_id: Specify the project_id if current context
                           is admin and admin wants to impact on
                           common user's tenant.
        """

        return self._driver.limit_check(context, self._resources, values,
                                        project_id=project_id)

    def reserve(self, context, expire=None, project_id=None, **deltas):
        """Check quotas and reserve resources.

        For counting quotas--those quotas for which there is a usage
        synchronization function--this method checks quotas against
        current usage and the desired deltas.  The deltas are given as
        keyword arguments, and current usage and other reservations
        are factored into the quota check.

        This method will raise a QuotaResourceUnknown exception if a
        given resource is unknown or if it does not have a usage
        synchronization function.

        If any of the proposed values is over the defined quota, an
        OverQuota exception will be raised with the sorted list of the
        resources which are too high.  Otherwise, the method returns a
        list of reservation UUIDs which were created.

        :param context: The request context, for access checks.
        :param expire: An optional parameter specifying an expiration
                       time for the reservations.  If it is a simple
                       number, it is interpreted as a number of
                       seconds and added to the current time; if it is
                       a datetime.timedelta object, it will also be
                       added to the current time.  A datetime.datetime
                       object will be interpreted as the absolute
                       expiration time.  If None is specified, the
                       default expiration time set by
                       --default-reservation-expire will be used (this
                       value will be treated as a number of seconds).
        :param project_id: Specify the project_id if current context
                           is admin and admin wants to impact on
                           common user's tenant.
        """

        reservations = self._driver.reserve(context, self._resources, deltas,
                                            expire=expire,
                                            project_id=project_id)

        LOG.debug(_("Created reservations %(reservations)s") %
                  {"reservations": reservations})

        return reservations

    def commit(self, context, reservations, project_id=None):
        """Commit reservations.

        :param context: The request context, for access checks.
        :param reservations: A list of the reservation UUIDs, as
                             returned by the reserve() method.
        :param project_id: Specify the project_id if current context
                           is admin and admin wants to impact on
                           common user's tenant.
        """

        try:
            self._driver.commit(context, reservations, project_id=project_id)
        except Exception:
            # NOTE(Vek): Ignoring exceptions here is safe, because the
            # usage resynchronization and the reservation expiration
            # mechanisms will resolve the issue.  The exception is
            # logged, however, because this is less than optimal.
            LOG.exception(_("Failed to commit reservations "
                            "%(reservations)s") %
                          {"reservations": reservations})

    def rollback(self, context, reservations, project_id=None):
        """Roll back reservations.

        :param context: The request context, for access checks.
        :param reservations: A list of the reservation UUIDs, as
                             returned by the reserve() method.
        :param project_id: Specify the project_id if current context
                           is admin and admin wants to impact on
                           common user's tenant.
        """

        try:
            self._driver.rollback(context, reservations, project_id=project_id)
        except Exception:
            # NOTE(Vek): Ignoring exceptions here is safe, because the
            # usage resynchronization and the reservation expiration
            # mechanisms will resolve the issue.  The exception is
            # logged, however, because this is less than optimal.
            LOG.exception(_("Failed to roll back reservations "
                            "%(reservations)s") %
                          {"reservations": reservations})

    def destroy_all_by_project(self, context, project_id):
        """Destroy all quotas, usages, and reservations associated with a

        project.

        :param context: The request context, for access checks.
        :param project_id: The ID of the project being deleted.
        """

        self._driver.destroy_all_by_project(context, project_id)

    def expire(self, context):
        """Expire reservations.

        Explores all currently existing reservations and rolls back
        any that have expired.

        :param context: The request context, for access checks.
        """

        self._driver.expire(context)

    @property
    def resources(self):
        return sorted(self._resources.keys())


QUOTAS = QuotaEngine()


resources = [
    ReservableResource('plans', None,
                       'quota_plans'),
    ReservableResource('checkpoints', None,
                       'quota_checkpoints'),
]


QUOTAS.register_resources(resources)


OVER_QUOTA_RESOURCE_EXCEPTIONS = {
    'plans': exception.PlanLimitExceeded,
    'checkpoints': exception.CheckpointLimitExceeded
}


def process_reserve_over_quota(context, over_quota_exception,
                               resource, size=None):
    """Handle OverQuota exception.

    Analyze OverQuota exception, and raise new exception related to
    resource type. If there are unexpected items in overs,
    UnexpectedOverQuota is raised.

    :param context: security context
    :param over_quota_exception: OverQuota exception
    :param resource: can be backups, snapshots, and volumes
    :param size: requested size in reservation
    """
    def _consumed(name):
        return usages[name]['reserved'] + usages[name]['in_use']

    overs = over_quota_exception.kwargs['overs']
    usages = over_quota_exception.kwargs['usages']
    quotas = over_quota_exception.kwargs['quotas']
    invalid_overs = []

    for over in overs:
        if (resource in OVER_QUOTA_RESOURCE_EXCEPTIONS.keys() and
                resource in over):
            msg = ("Quota exceeded for %(s_pid)s, tried to create "
                   "%(s_resource)s (%(d_consumed)d %(s_resource)ss "
                   "already consumed).")
            LOG.warning(msg, {'s_pid': context.project_id,
                              'd_consumed': _consumed(over),
                              's_resource': resource[:-1]})
            raise OVER_QUOTA_RESOURCE_EXCEPTIONS[resource](
                allowed=quotas[over],
                name=over)
        invalid_overs.append(over)

    if invalid_overs:
        raise exception.UnexpectedOverQuota(name=', '.join(invalid_overs))

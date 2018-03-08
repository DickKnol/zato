# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function, unicode_literals

# stdlib
from contextlib import closing
from datetime import datetime, timedelta
from functools import wraps
from inspect import getargspec
from logging import getLogger
from random import randint

# SQALchemy
from sqlalchemy import asc, desc, func
from sqlalchemy.sql import and_ as sql_and, or_ as sql_or

# Zato
from zato.common.odb.model import SSOUser
from zato.common.odb.query import query_wrapper
from zato.common.util.search import SearchResults as _SearchResults
from zato.common.util.sql import search as util_search
from zato.server.service import Service
from zato.sso import const
from zato.sso.odb.query import _user_basic_columns

# ################################################################################################################################

_does_not_exist = object()

# ################################################################################################################################

name_op_allowed = set(const.search())
name_op_sa = {
    const.search.and_: sql_and,
    const.search.or_: sql_or,
}

# ################################################################################################################################

class OrderBy(object):
    """ Defaults for the SQL ORDER BY clause.
    """
    def __init__(self):
        self.asc = 'asc'
        self.desc = 'desc'

        # All ORDER BY directions allowed
        self.dir_allowed = set((self.asc, self.desc))

        # All columns that results may be ordered by
        self.out_columns_allowed = set(('display_name', 'username', 'sign_up_time', 'user_id'))

        # How results will be sorted if no user-defined order is given
        self.default = (
            asc('display_name'),
            asc('username'),
            asc('sign_up_time'),
            asc('user_id'),
        )

# ################################################################################################################################

class SSOSearch(object):
    """ SSO search functions, constants and defaults.
    """
    def __init__(self):
        self.out_columns = []
        self.order_by = OrderBy()

        # Maps publicly visible column names to SQL ones
        self.name_columns = {
            'display_name': SSOUser.display_name_upper,
            'first_name': SSOUser.first_name_upper,
            'middle_name': SSOUser.middle_name_upper,
            'last_name': SSOUser.last_name_upper,
        }

        # Maps columns and exactness flags to sqlalchemy-level functions that look up data
        self._name_column_op = {

            (SSOUser.display_name_upper, True): SSOUser.display_name_upper.__eq__,
            (SSOUser.first_name_upper, True)  : SSOUser.first_name_upper.__eq__,
            (SSOUser.middle_name_upper, True) : SSOUser.middle_name_upper.__eq__,
            (SSOUser.last_name_upper, True)   : SSOUser.last_name_upper.__eq__,

            (SSOUser.display_name_upper, False): SSOUser.display_name_upper.contains,
            (SSOUser.first_name_upper, False)  : SSOUser.first_name_upper.contains,
            (SSOUser.middle_name_upper, False) : SSOUser.middle_name_upper.contains,
            (SSOUser.last_name_upper, False)   : SSOUser.last_name_upper.contains,
        }

        self._non_name_column_op = {
            'email': SSOUser.email.__eq__,
            'sign_up_status': SSOUser.sign_up_status.__eq__,
            'approval_status': SSOUser.approval_status.__eq__,
        }

        self._user_id_op = SSOUser.user_id.__eq__
        self._username_op = SSOUser.username.__eq__

# ################################################################################################################################

    def set_up(self):
        for column in _user_basic_columns:
            self.out_columns.append(getattr(SSOUser, column.name))

# ################################################################################################################################

    @query_wrapper
    def sql_search_func(self, session, ignored_cluster_id, order_by, needs_columns=False):
        return session.query(*self.out_columns).\
               order_by(*order_by)

# ################################################################################################################################

    def _get_order_by(self, order_by):
        """ Constructs an ORDER BY clause for the user search query.
        """
        out = []

        for item in order_by:
            items = item.items()
            if len(items) != 1 or len(items[0]) != 2:
                raise ValueError('Invalid order_by config `{}`'.format(items))
            else:
                column, dir = items[0]

                if column not in self.order_by.columns_allowed:
                    raise ValueError('Invalid order_by column `{}`'.format(column))

                if dir not in self.order_by.dir_allowed:
                    raise ValueError('Invalid order_by dir `{}`'.format(dir))

                # Columns and directions are valid, we can construct the ORDER BY clause now

                func = asc if dir == self.order_by.asc else desc
                out.append(func(column))

        return out

# ################################################################################################################################

    def _get_where_user_id(self, user_id):
        """ Constructs a WHERE clause to look up users by user_id (will return at most one row).
        """
        return self._user_id_op(user_id)

# ################################################################################################################################

    def _get_where_username(self, username):
        """ Constructs a WHERE clause to look up users by username (will return at most one row).
        """
        return self._username_op(username)

# ################################################################################################################################

    def _get_where_name(self, name, name_exact, name_op, name_op_allowed=name_op_allowed, name_op_sa=name_op_sa):
        """ Constructs a WHERE clause to look up users by display/first/middle or last name.
        """
        name_criteria_raw = []
        name_criteria = []
        name_where = None

        # Name must be a dict of columns
        if not isinstance(name, dict):
            raise ValueError('Invalid name `{}`'.format(name))

        # Validate that only allowed columns and values of expected type are passed to the name filter
        for column_key, value in name.items():
            if column_key not in self.name_columns:
                raise ValueError('Invalid name key `{}`'.format(column_key))
            elif not isinstance(value, basestring):
                raise ValueError('Invalid value `{}`'.format(value))
            else:
                value = value.strip()
                if not value:
                    raise ValueError('Value must not be empty, key `{}`'.format(column_key))
                name_criteria_raw.append((self.name_columns[column_key], value.upper()))

        # Name operator is needed only if name is given on input
        if name_op:
            if name_op not in name_op_allowed:
                raise ValueError('Invalid name_op `{}`'.format(name_op))
        else:
            name_op = const.search.and_

        # Convert a label to an actual SQALchemy-level function
        name_op = name_op_sa[name_op]

        # We need to have a reference to a Python-level function suc

        # At this point we know all name-related input is correct and we have both criteria
        # and an operator to joined them with.
        if name_criteria_raw:
            for column, value in name_criteria_raw:
                func = self._name_column_op[(column, name_exact)]
                name_criteria.append(func(value))

            name_where = name_op(*name_criteria)

        return name_where

# ################################################################################################################################

    def _get_where_non_name(self, config):
        """ Constructs a WHERE clause for criteria other than display-related names.
        """
        # We can search by email only if emails are not encrypted
        email = config.get('email')
        if email:
            if not config.get('email_search_enabled'):
                raise ValueError('Cannot look up users by email')

        non_name_where = None
        non_name_criteria = []

        for non_name in self._non_name_column_op:
            value = config.get(non_name, _does_not_exist)
            if value is not _does_not_exist:
                if value is not None:
                    if not isinstance(value, basestring):
                        raise ValueError('Invalid value `{}`'.format(value))
                non_name_criteria.append(self._non_name_column_op[non_name](value))

        if non_name_criteria:
            non_name_where = sql_and(*non_name_criteria)

        return non_name_where

# ################################################################################################################################

    def _get_where(self, config):
        """ Creates the WHERE part of a user search query.
        """
        # Check shorter paths first
        username = config.get('username')
        user_id = config.get('user_id')

        # If either username or user_id are given on input, we cut it short and ignore
        # other parameters - this is because both of them are unique in the DB so any other
        # parameter would have no influence over results ..

        # .. also, if both of them are given on input, we only take user_id into account.
        if username or user_id:
            if user_id:
                return self._get_where_user_id(user_id)
            else:
                return self._get_where_username(username)

        # If we are here it means there was neither user_id nor username on input

        # Should we search by any name?
        name_where = None

        # Should we search by any non-name criteria?
        non_name_where = None

        # Get all name-related criteria
        if 'name' in config:
            name_where = self._get_where_name(config.get('name'), config.get('name_exact'), config.get('name_op'))

        # Get all non-name related criteria
        non_name_where = self._get_where_non_name(config)

        # Now we have both name-related and non-name related WHERE conditions, yet both possibly empty,
        # but even if they are empty we can construct the final AND-joined WHERE condition now.

        # Names are given ..
        if name_where is not None:

            # .. but other attributes are not, in this case WHERE will be names only.
            if non_name_where is None:
                where = name_where

            # .. there are both names and non-names.
            else:
                where = sql_and(name_where, non_name_where)

        # .. there are only non-names.
        elif non_name_where is not None:
            where = non_name_where

        return where

# ################################################################################################################################

    def search(self, session, config):
        """ Looks up users with the configuration given on input.
        """
        # WHERE clause
        where = self._get_where(config)

        # ORDER BY clause
        order_by = config.get('order_by')
        order_by = self._get_order_by(order_by) if order_by else self.order_by.default

        return util_search(self.sql_search_func, config, [], session, None, order_by, False, where=where)

# ################################################################################################################################

sso_search = SSOSearch()
sso_search.set_up()

# ################################################################################################################################

def sql_search(self, data, current_ust, current_app, remote_addr):
    """ Looks up users by specific search criteria from the 'data' dictionary.
    Must be called with a UST belonging to a super-user.
    """
    current_session = self._get_current_session(current_ust, current_app, remote_addr, needs_super_user=True)

    config = {
        'paginate': True,
        'page_size': 2,
        'email_search_enabled': not self.sso_conf.main.encrypt_email,
        'name_op': const.search.and_,
        'name_exact': False,
        'name': {
            'last_name': 'smith'
        },
        'sign_up_status': 'zzz',
    }

    with closing(self.odb_session_func()) as session:
        out = sso_search.search(session, config)

        for row in out.result:
            print(row._asdict())

# ################################################################################################################################

class MyService(Service):
    def handle(self):

        username = 'admin1'
        password = '****'
        session = self.sso.user.login(username, password, 'CRM', '127.0.0.1', 'my-user-agent', False, False)

        # Request metadata
        current_ust = session.ust
        current_app = 'CRM'
        remote_addr = '127.0.0.1'

        data = {}
        sql_search(self.sso.user, data, current_ust, current_app, remote_addr)

# ################################################################################################################################

# -*- coding: utf-8 -*-

"""
Copyright (C) 2019, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

from __future__ import absolute_import, division, print_function, unicode_literals

# stdlib
from logging import getLogger
from traceback import format_exc

# Zato
from zato.common import NotGiven
from zato.common.exception import BadRequest, InternalServerError

# ################################################################################################################################

# Type checking
import typing

if typing.TYPE_CHECKING:

    # stdlib
    from typing import Callable

    # Bunch
    from bunch import Bunch

    # For pyflakes
    Bunch = Bunch
    Callable = Callable

# ################################################################################################################################

logger = getLogger(__name__)

# ################################################################################################################################

json_rpc_version_supported = '2.0'

# ################################################################################################################################
# ################################################################################################################################

class RequestContext(object):
    __slots__ = ('cid', 'orig_message', 'message')

    def __init__(self):
        self.cid = None          # type: unicode
        self.orig_message = None # type: object
        self.message = None      # type: unicode

# ################################################################################################################################
# ################################################################################################################################

class ErrorCtx(object):
    __slots__ = ('cid', 'code', 'message')

    def __init__(self):
        self.cid = None     # type: unicode
        self.code = None    # type: int
        self.message = None # type: unicode

    def to_dict(self):
        # type: () -> dict
        return {
            'code': self.code,
            'message': self.message,
            'data': {
                'ctx': {
                    'cid': self.cid
                }
            }
        }

# ################################################################################################################################
# ################################################################################################################################

class ItemResponse(object):
    __slots__ = ('id', 'cid', 'error', 'result')

    def __init__(self):
        self.id = None     # type: int
        self.cid = None    # type: unicode
        self.error = None  # type: ErrorCtx
        self.result = None # type: object

    def to_dict(self, _json_rpc_version=json_rpc_version_supported):
        # type: (unicode) -> dict

        out = {
            'jsonrpc': _json_rpc_version,
            'id': self.id,
        }

        if self.result:
            out['result'] = self.result
        else:
            out['error'] = self.error.to_dict()

        return out

# ################################################################################################################################
# ################################################################################################################################

class JSONRPCException(object):
    code = -32000

# ################################################################################################################################
# ################################################################################################################################

class JSONRPCBadRequest(JSONRPCException, BadRequest):
    def __init__(self, cid, message):
        # type: (unicode, unicode)
        BadRequest.__init__(self, cid, msg=message)

# ################################################################################################################################
# ################################################################################################################################

class InvalidRequest(JSONRPCBadRequest):
    code = -32600

# ################################################################################################################################
# ################################################################################################################################

class MethodNotFound(JSONRPCBadRequest):
    code = -32601

# ################################################################################################################################
# ################################################################################################################################

class InternalError(JSONRPCException, InternalServerError):
    code = -32603

# ################################################################################################################################
# ################################################################################################################################

class ParseError(JSONRPCBadRequest):
    code = -32700

# ################################################################################################################################
# ################################################################################################################################

class JSONRPCItem(object):
    """ An object describing an individual JSON-RPC request.
    """
    __slots__ = 'jsonrpc', 'method', 'params', 'id', 'needs_response'

# ################################################################################################################################

    def __init__(self):
        self.jsonrpc = None # type: unicode
        self.method = None  # type: unicode
        self.params = None  # type: object
        self.id = None      # type: unicode
        self.needs_response = None # type: bool

# ################################################################################################################################

    def to_dict(self):
        # type: () -> dict
        return {
            'jsonrpc': self.jsonrpc,
            'method': self.method,
            'params': self.params,
            'id': self.id
        }

# ################################################################################################################################

    @staticmethod
    def from_dict(item):
        # type: (dict) -> JSONRPCItem

        # Our object to return
        out = JSONRPCItem()

        # At this stage we only create a Python-level object and input
        # validation is performed by our caller.
        out.jsonrpc = item.get('jsonrpc')
        out.id = item.get('id', NotGiven)
        out.method = item.get('method')
        out.params = item.get('params')
        out.needs_response = out.id is not NotGiven

        return out

# ################################################################################################################################
# ################################################################################################################################

class JSONRPCHandler(object):
    def __init__(self, config, invoke_func):
        # type: (Bunch, Callable)
        self.config = config
        self.invoke_func = invoke_func

# ################################################################################################################################

    def handle(self, ctx):
        # type: (RequestContext) -> object
        if isinstance(ctx.message, list):
            return self.handle_list(ctx)
        else:
            return self.handle_one_item(ctx)

# ################################################################################################################################

    def can_handle(self, method):
        # type: (unicode) -> bool
        return method in self.config['service_whitelist']

# ################################################################################################################################

    def _handle_one_item(self, cid, message, orig_message, _json_rpc_version=json_rpc_version_supported):
        # type: (RequestContext, unicode) -> dict

        try:
            # Response to return
            out = ItemResponse()

            # Construct a Python object out of incoming data
            item = JSONRPCItem.from_dict(message)

            # We should have the ID at this point
            out.id = item.id

            # Confirm that we can handle the JSON-RPC version requested
            if item.jsonrpc != json_rpc_version_supported:
                raise InvalidRequest(cid, 'Unsupported JSON-RPC version `{}` in `{}`'.format(item.jsonrpc, orig_message))

            # Confirm that method requested is one that we can handle
            if not self.can_handle(item.method):
                raise MethodNotFound(cid, 'Method not supported `{}` in `{}`'.format(item.method, orig_message))

            # Try to invoke the service ..
            service_response = self.invoke_func(item.method, item.params, skip_response_elem=True)

            # .. no exception here = invocation was successful
            out.result = service_response

            return out.to_dict() if item.needs_response else None

        except Exception as e:
            # We treat any exception at this point as an internal error
            logger.warn('JSON-RPC exception in `%s` (%s); msg:`%s`, e:`%s`', self.config.name, cid, orig_message, format_exc())

            error_ctx = ErrorCtx()
            error_ctx.cid = cid

            if isinstance(e, JSONRPCException):
                err_code = e.code
                err_message = e.message
            else:
                err_code = -32000
                err_message = 'Message could not be handled'

            error_ctx.code = err_code
            error_ctx.message = err_message

            out.error = error_ctx

            return out.to_dict()

# ################################################################################################################################

    def handle_one_item(self, ctx, _json_rpc_version=json_rpc_version_supported):
        # type: (RequestContext) -> dict
        return self._handle_one_item(ctx.cid, ctx.message, ctx.orig_message)

# ################################################################################################################################

    def handle_list(self, ctx):
        # type: (RequestContext) -> list
        out = []

        for item in ctx.message: # type: dict
            out.append(self._handle_one_item(ctx.cid, item, ctx.orig_message))

        return out

# ################################################################################################################################
# ################################################################################################################################

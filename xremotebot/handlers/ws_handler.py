# -*- coding: utf8 -*-

'''Manejador de conexiones con websockets
por el momento es un echo server
'''
import logging

import tornado.websocket
import tornado.escape
from xremotebot.lib.message import value, error, valid_client_message
from xremotebot.configuration import public_server
from xremotebot.models.global_entity import Global
from xremotebot.models.robot_entity import Robot
from xremotebot.models.reservation import Reservation

logger = logging.getLogger('xremotebot')

import re
import collections
import datetime
API_Handler = collections.namedtuple(
    'API_Handler',
    ('klass', 'allowed_methods')
)
public = re.compile(r'(^[a-zA-Z]\w*[a-zA-Z0-9]$|^[a-zA-Z]$)')


class WSHandler(tornado.websocket.WebSocketHandler):
    handlers = {}

    def __init__(self, *args, **kwargs):
        # FIXME
        super(WSHandler, self).__init__(*args, **kwargs)

    def open(self):
        self.authenticated = False

    def on_close(self):
        user = self.current_user
        handler = self.handlers['robot']
        if user is not None:
            for reservation in user.reservations:
                model = reservation.robot_model
                id_ = reservation.robot_id
                try:
                    handler.klass._send(
                        'stop',
                        self,
                        {
                            'robot_model': model,
                            'robot_id': id_,
                        }
                    )
                except Exception as e:
                    logger.error('Stopping robot after'
                                 'disconnection: %s', e.message)
                reservation.cancel()

    def on_message(self, message):
        try:
            command = tornado.escape.json_decode(message)
        except ValueError:
            logger.exception('Error trying to decode client message')
            self.write_message(error('Error decoding client message'))
            return

        valid, error_msg = valid_client_message(command)
        if not valid:
            logger.warning(error_msg)
            self.write_message(error_msg)
            return

        command['msg_id'] = command.get('msg_id', None)
        command['args'] = command.get('args', [])

        allowed, errmsg = self._user_authorized(
            command['entity'],
            command['method'],
            *command['args']
        )
        if allowed:
            self._handle_api_message(command)
        else:
            self.write_message(error(errmsg, command['msg_id']))

    def get_current_user(self):
        if not self.authenticated:
            return None

        return self.user

    def set_current_user(self, user):
        self.authenticated = True
        self.user = user

    def _user_authorized(self, entity, method, *args):
        '''
        Returns a tuple where the first value if true if the user can
        perform this action, and false otherwise. The second value
        is an error message if the user can't perform this action
        '''
        if not public_server:
            return (True, None)

        if entity == 'global' and \
                method in ('authentication_required', 'authenticate'):
            # You can always ask if auth is required and
            # authenticate
            return (True, None)
        elif self.authenticated:
            if entity == 'global':
                # When authenticated you can do anithing with global
                return (True, None)
            elif entity == 'robot':
                # If this is a robot check if it is reserved by this user
                reserved = Reservation.reserved(
                    self.current_user,
                    args[0]['robot_model'],
                    args[0]['robot_id'],
                    datetime.datetime.now(),
                    datetime.datetime.now()
                )
                if len(reserved) > 0:
                    return (True, None)
                else:
                    return (False, 'There is no active reservation for ' +
                            str(args[0]))

        return (False,
                'Authentication required for {}.{}({})'.format(entity,
                                                               method,
                                                               args))

    def _handle_api_message(self, json_msg):
        entity = json_msg['entity']
        method = json_msg['method']
        msg_id = json_msg.get('msg_id', None)
        args = json_msg.get('args', [])

        handler = self.handlers.get(entity, None)
        # FIXME
        logger.info('{%s}', method)
        logger.info(handler)
        if handler is None:
            logger.info('"%s" entity not supported', entity)
            self.write_message(error('"{}" entity not supported'.format(entity)))
        if method not in handler.allowed_methods:
            logger.info('"%s" method not supported by "%s" handler with allowed methods %s',
                    method, type(handler.klass), list(handler.allowed_methods))
            self.write_message(error('"{}" method not supported by "{}" handler'.format(method, str(handler))))

        try:
            msg = handler.klass._send(method, self, *args)
        except Exception as e:
            logger.error('Error dispatching %s: %s %s',
                         method,
                         e.__class__.__name__,
                         e.message)
            self.write_message(error(
                '{}: {}'.format(e.__class__.__name__, e.message), msg_id))
        else:
            is_delayed, time_arg = handler.klass._delayed_stop(method, *args)
            if (is_delayed and time_arg < len(args) and
                    args[time_arg] is not None and args[time_arg] >= 0):
                time = args[time_arg]
                logger.debug('About to sleep %d seconds', time)

                def delayed_f():
                    try:
                        handler.klass.stop(self, args[0])
                    except Exception as e:
                        logger.error('Error dispatching %s: %s',
                                     method, e.message)
                        response = error(
                            '{}: {}'.format(
                                e.__class__.__name__,
                                e.message
                            ),
                            msg_id
                        )
                    else:
                        response = value(msg, msg_id)

                    try:
                        self.write_message(response)
                    except tornado.websocket.WebSocketClosedError as e:
                        logger.info('WebSocket closed while sending the'
                                    'response to a delayed action')

                tornado.ioloop.IOLoop.current().call_later(time, delayed_f)
            else:
                self.write_message(value(msg, msg_id))

    @classmethod
    def register_api_handler(cls, entity, entity_handler):
        cls.handlers[entity] = API_Handler(entity_handler, tuple(filter(public.match, dir(entity_handler))))
        # FIXME debug ineficiente
        logger.debug("%s entity handled by instance of %s with public methods %s", entity,
                type(entity_handler), list(cls.handlers[entity].allowed_methods))


WSHandler.register_api_handler('global', Global())
WSHandler.register_api_handler('robot', Robot())

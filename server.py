from gevent import socket
from gevent.pool import Pool
from gevent.server import StreamServer

from collections import namedtuple
from io import BytesIO
import os
from socket import error as socket_error
import logging
import time

logger = logging.getLogger(__name__)


class CommandError(Exception): pass
class Disconnect(Exception): pass

Error = namedtuple('Error', ('message',))

unicode = str

class ProtocolHandler(object):
    def __init__(self):
        self.handlers = {
            b'+': self.handle_simple_string,
            b'-': self.handle_error,
            b':': self.handle_integer,
            b'$': self.handle_string,
            b'^': self.handle_unicode,
            b'*': self.handle_array,
            b'%': self.handle_dict,
        }

    def handle_request(self, socket_file):
        first_byte = socket_file.read(1)
        if not first_byte:
            raise Disconnect()

        try:
            # Delegate to the appropriate handler based on the first byte.
            return self.handlers[first_byte](socket_file)
        except KeyError:
            raise CommandError('bad request')

    def handle_simple_string(self, socket_file):
        return socket_file.readline().rstrip(b'\r\n')

    def handle_error(self, socket_file):
        return Error(socket_file.readline().rstrip(b'\r\n'))

    def handle_integer(self, socket_file):
        return socket_file.readline().rstrip(b'\r\n')

    def handle_string(self, socket_file):
        # First read the length ($<length>\r\n).
        length = int(socket_file.readline().rstrip(b'\r\n'))
        if length == -1:
            return None  # Special-case for NULLs.
        length += 2  # Include the trailing \r\n in count.
        return socket_file.read(length)[:-2]
    
    def handle_unicode(self, socket_file):
        return self.handle_string(socket_file).decode('utf-8')

    def handle_array(self, socket_file):
        num_elements = int(socket_file.readline().rstrip(b'\r\n'))
        return [self.handle_request(socket_file) for _ in range(num_elements)]
    
    def handle_dict(self, socket_file):
        num_items = int(socket_file.readline().rstrip(b'\r\n'))
        elements = [self.handle_request(socket_file)
                    for _ in range(num_items * 2)]
        return dict(zip(elements[::2], elements[1::2]))

    def write_response(self, socket_file, data):
        buf = BytesIO()
        self._write(buf, data)
        buf.seek(0)
        socket_file.write(buf.getvalue())
        socket_file.flush()

    def _write(self, buf, data):

        if isinstance(data, bytes):
            buf.write(b'$%d\r\n%s\r\n' % (len(data), data))
        elif isinstance(data, unicode):
            bdata = data.encode('utf-8')
            buf.write(b'^%d\r\n%s\r\n' % (len(bdata), bdata))
        elif isinstance(data, int):
            buf.write(b':%d\r\n' % data)
        elif isinstance(data, Error):
            buf.write(b'-%s\r\n' % error.message)
        elif isinstance(data, (list, tuple)):
            buf.write(b'*%d\r\n' % len(data))
            for item in data:
                self._write(buf, item)
        elif isinstance(data, dict):
            buf.write(b'%%%d\r\n' % len(data))
            for key in data:
                self._write(buf, key)
                self._write(buf, data[key])
        elif data is None:
            buf.write(b'$-1\r\n')
        else:
            raise CommandError('unrecognized type: %s' % type(data))


class Server(object):
    def __init__(self, host='127.0.0.1', port=31337, max_clients=64):
        self._pool = Pool(max_clients)
        self._server = StreamServer(
            (host, port),
            self.connection_handler,
            spawn=self._pool)

        self._protocol = ProtocolHandler()
        self._kv = {}

        self._commands = self.get_commands()

    def get_commands(self):
        return {
            b'GET': self.get,
            b'SET': self.set,
            b'DELETE': self.delete,
            b'FLUSH': self.flush,
            b'MGET': self.mget,
            b'MSET': self.mset}

    def connection_handler(self, conn, address):
        logger.info('Connection received: %s:%s' % address)
        # Convert "conn" (a socket object) into a file-like object.
        socket_file = conn.makefile('rwb')

        # Process client requests until client disconnects.
        while True:
            try:
                data = self._protocol.handle_request(socket_file)
            except Disconnect:
                logger.info('Client went away: %s:%s' % address)
                break

            try:
                resp = self.get_response(data)
            except CommandError as exc:
                logger.exception('Command error')
                resp = Error(exc.args[0])

            self._protocol.write_response(socket_file, resp)

    def run(self):
        self._server.serve_forever()

    def get_response(self, data):
        if not isinstance(data, list):
            try:
                data = data.split()
            except:
                raise CommandError('Request must be list or simple string.')

        if not data:
            raise CommandError('Missing command')

        command = data[0].upper()
        if command not in self._commands:
            raise CommandError('Unrecognized command: %s' % command)
        else:
            logger.debug('Received %s', command)

        return self._commands[command](*data[1:])

    def get(self, key):
        return self._kv.get(key)

    def set(self, key, value):
        self._kv[key] = value
        return 1

    def delete(self, key):
        if key in self._kv:
            del self._kv[key]
            return 1
        return 0

    def flush(self):
        kvlen = len(self._kv)
        self._kv.clear()
        return kvlen

    def mget(self, *keys):
        return [self._kv.get(key) for key in keys]

    def mset(self, *items):
        data = zip(items[::2], items[1::2])
        for key, value in data:
            self._kv[key] = value
        return len(data)


class Client(object):
    def __init__(self, host='127.0.0.1', port=31337):
        self._protocol = ProtocolHandler()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.connect((host, port))
        self._fh = self._socket.makefile('rwb')

    def execute(self, *args):
        tic = time.perf_counter()
        self._protocol.write_response(self._fh, args)
        resp = self._protocol.handle_request(self._fh)
        toc = time.perf_counter()

        print(f'Command ran in {toc - tic:0.8f} seconds')

        if isinstance(resp, Error):
            raise CommandError(resp.message)
        return resp

    def get(self, key):
        return self.execute(b'GET', key)

    def set(self, key, value):
        return self.execute(b'SET', key, value)

    def delete(self, key):
        return self.execute(b'DELETE', key)

    def flush(self):
        return self.execute(b'FLUSH')

    def mget(self, *keys):
        return self.execute(b'MGET', *keys)

    def mset(self, *items):
        return self.execute(b'MSET', *items)


if __name__ == '__main__':
    from gevent import monkey; monkey.patch_all()
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.DEBUG)
    Server().run()
#!/var/ossec/framework/python/bin/python3

# Copyright (C) 2015-2020, Wazuh Inc.
# Created by Wazuh, Inc. <info@wazuh.com>.
# This program is a free software; you can redistribute it and/or modify it under the terms of GPLv2

import argparse
import asyncio
import os
import ssl
import sys
from collections import deque

import aiohttp_cache
import aiohttp_cors
import connexion
import psutil
import uvloop
from aiohttp_swagger import setup_swagger

from api import alogging, configuration, __path__ as api_path
# noinspection PyUnresolvedReferences
from api import validator
from api.api_exception import APIException
from api.constants import CONFIG_FILE_PATH, API_LOG_FILE_PATH
from api.middlewares import set_user_name
from api.util import to_relative_path
from wazuh import pyDaemonModule, common
from wazuh.core.cluster import __version__, __author__, __ossec_name__, __licence__


def set_logging(foreground_mode=False, debug_mode='info'):
    for logger_name in ('connexion.aiohttp_app', 'connexion.apis.aiohttp_api', 'wazuh'):
        api_logger = alogging.APILogger(log_path='logs/api.log', foreground_mode=foreground_mode,
                                        debug_level=debug_mode,
                                        logger_name=logger_name)
        api_logger.setup_logger()


def print_version():
    print("\n{} {} - {}\n\n{}".format(__ossec_name__, __version__, __author__, __licence__))


def start(foreground, root, config_file):
    """
    Run the Wazuh API.

    If another Wazuh API is running, this function fails. The `stop` command should be used first.
    This function exits with 0 if success or 2 if failed because the API was already running.

    Arguments
    ---------
    foreground : bool
        If the API must be daemonized or not
    root : bool
        If true, the daemon is run as root. Normally not recommended for security reasons
    config_file : str
        Path to the API config file
    """

    pids = get_wazuh_apid_pids()
    if pids:
        print(f"Cannot start API while other processes are running. Kill these before {pids}")
        sys.exit(2)

    # Foreground/Daemon
    if not foreground:
        pyDaemonModule.pyDaemon()

    # Drop privileges to ossec
    if not root:
        os.setgid(common.ossec_gid())
        os.setuid(common.ossec_uid())

    api_config = configuration.read_api_config(config_file=config_file)
    cors = api_config['cors']

    set_logging(debug_mode=api_config['logs']['level'], foreground_mode=foreground)

    # set correct permissions on api.log file
    if os.path.exists('{0}/logs/api.log'.format(common.ossec_path)):
        os.chown('{0}/logs/api.log'.format(common.ossec_path), common.ossec_uid(), common.ossec_gid())
        os.chmod('{0}/logs/api.log'.format(common.ossec_path), 0o660)

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    app = connexion.AioHttpApp(__name__, host=api_config['host'],
                               port=api_config['port'],
                               specification_dir=os.path.join(api_path[0], 'spec'),
                               options={"swagger_ui": False}
                               )
    app.add_api('spec.yaml',
                arguments={'title': 'Wazuh API',
                           'protocol': 'https' if api_config['https']['enabled'] else 'http',
                           'host': api_config['host'],
                           'port': api_config['port']
                           },
                strict_validation=True,
                validate_responses=True,
                pass_context_arg_name='request',
                options={"middlewares": [set_user_name]})
    # Enable CORS
    if cors:
        aiohttp_cors.setup(app.app)

    # Enable cache plugin
    aiohttp_cache.setup_cache(app.app)

    # Enable swagger UI plugin
    setup_swagger(app.app,
                  ui_version=3,
                  swagger_url='/ui',
                  swagger_from_file=os.path.join(app.specification_dir, 'spec.yaml'))

    # Configure https
    if api_config['https']['enabled']:
        try:
            ssl_context = ssl.SSLContext()
            if api_config['https']['use_ca']:
                ssl_context.verify_mode = ssl.CERT_REQUIRED
                ssl_context.load_verify_locations(api_config['https']['ca'])
            ssl_context.load_cert_chain(certfile=api_config['https']['cert'],
                                        keyfile=api_config['https']['key'])
        except ssl.SSLError as e:
            raise APIException(2003, details='Private key does not match with the certificate')
        except IOError as e:
            raise APIException(2003, details='Please, ensure '
                                             'if path to certificates is correct in '
                                             'the configuration file '
                                             f'(WAZUH_PATH/{to_relative_path(CONFIG_FILE_PATH)})')
    else:
        ssl_context = None

    app.run(port=api_config['port'],
            host=api_config['host'],
            ssl_context=ssl_context,
            access_log_class=alogging.AccessLogger,
            use_default_access_log=True
            )


def stop():
    """
    Stop the Wazuh API

    This function applies when the API is running in daemon mode.
    """
    def on_terminate(p):
        print(f"Wazuh API process {p.pid} terminated.")

    pids = get_wazuh_apid_pids()
    if pids:
        procs = [psutil.Process(pid=pid) for pid in pids]
        for proc in procs:
            proc.terminate()
        gone, alive = psutil.wait_procs(procs=procs, timeout=5, callback=on_terminate)
        for proc in alive:
            proc.kill()


def restart(foreground, root, config_file):
    """
    Restart the API by calling the `stop` and `start` functions respectively.

    Arguments
    ---------
    foreground : bool
        If the API must be daemonized or not
    root : bool
        If true, the daemon is run as root. Normally not recommended for security reasons
    config_file : str
        Path to the API config file
    """
    print("Restarting Wazuh API")
    stop()
    start(foreground, root, config_file)
    print("Wazuh API restarted")


def status():
    """
    Print the current status of the API daemon.
    """
    if get_wazuh_apid_pids():
        print("Wazuh API is running")
    else:
        print("Wazuh API is stopped.")
        with open(API_LOG_FILE_PATH, 'r') as log:
            for line in deque(log, 20):
                print(line)
        print(f"Full log in {API_LOG_FILE_PATH}")


def get_wazuh_apid_pids():
    """
    Get the API service pid.

    This function applies when the API is running as a daemon.

    Returns
    -------
    list
        List with all the pids of API processes. None if no one is found.
    """
    result = []
    for process in psutil.process_iter(attrs=['pid', 'name']):
        if process.pid != os.getpid() and process.info['name'] == 'python3':
            if 'wazuh-apid.py' in ' '.join(process.cmdline()):
                result.append(process.pid)
    return result if len(result) > 0 else None


def test_config(config_file):
    """
    Make an attempt to read the API config file

    Exits with 0 code if sucess, 1 otherwise.

    Arguments
    ---------
    config_file : str
        Path of the file
    """
    try:
        configuration.read_api_config(config_file=config_file)
    except Exception as e:
        print(f"Configuration not valid: {e}")
        sys.exit(1)
    sys.exit(0)


def version():
    """
    Print API version and exits with 0 code.
    """
    print_version()
    sys.exit(0)


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    ####################################################################################################################
    parser.add_argument('action', help="Action to be performed", choices=('start', 'stop', 'restart',
                                                                          'status', 'test_config', 'version'),
                        default='start', nargs='?')
    parser.add_argument('-f', help="Run in foreground", action='store_true', dest='foreground')
    parser.add_argument('-V', help="Print version", action='store_true', dest="version")
    parser.add_argument('-t', help="Test configuration", action='store_true', dest='test_config')
    parser.add_argument('-r', help="Run as root", action='store_true', dest='root')
    parser.add_argument('-c', help="Configuration file to use", type=str, metavar='config', dest='config_file',
                        default=common.api_config_path)
    args = parser.parse_args()

    if args.action == 'start':
        start(args.foreground, args.root, args.config_file)
    elif args.action == 'stop':
        stop()
    elif args.action == 'restart':
        restart(args.foreground, args.root, args.config_file)
    elif args.action == 'status':
        status()
    elif args.action == 'test_config':
        test_config(args.config_file)
    elif args.action == 'version':
        version()
    else:
        print("Invalid action")
        sys.exit(1)

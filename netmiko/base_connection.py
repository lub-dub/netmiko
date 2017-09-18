'''
Base connection class for netmiko

Handles SSH connection and methods that are generically applicable to different
platforms (Cisco and non-Cisco).

Also defines methods that should generally be supported by child classes
'''

from __future__ import print_function
from __future__ import unicode_literals

import paramiko
import telnetlib
import time
import socket
import re
import io
from os import path
from threading import Lock

from netmiko.netmiko_globals import MAX_BUFFER, BACKSPACE_CHAR
from netmiko.ssh_exception import NetMikoTimeoutException, NetMikoAuthenticationException
from netmiko.utilities import write_bytes
from netmiko.py23_compat import string_types
from netmiko import log


class BaseConnection(object):
    """
    Defines vendor independent methods.

    Otherwise method left as a stub method.
    """
    def __init__(self, ip='', host='', username='', password='', secret='', port=None,
                 device_type='', verbose=False, global_delay_factor=1, use_keys=False,
                 key_file=None, allow_agent=False, ssh_strict=False, system_host_keys=False,
                 alt_host_keys=False, alt_key_file='', ssh_config_file=None, timeout=8,
                 session_timeout=60, keepalive=0):
        """
        Initialize attributes for establishing connection to target device.

        :param ip: IP address of target device. Not required if `host` is
            provided.
        :type ip: str
        :param host: Hostname of target device. Not required if `ip` is
                provided.
        :type host: str
        :param username: Username to authenticate against target device if
                required.
        :type username: str
        :param password: Password to authenticate against target device if
                required.
        :type password: str
        :param secret: The enable password if target device requires one.
        :type secret: str
        :param port: The destination port used to connect to the target
                device.
        :type port: int or None
        :param device_type: Class selection based on device type.
        :type device_type: str
        :param verbose: Enable additional messages to standard output.
        :type verbose: bool
        :param global_delay_factor: Multiplication factor affecting Netmiko delays (default: 1).
        :type global_delay_factor: int
        :param use_keys: Connect to target device using SSH keys.
        :type use_keys: bool
        :param key_file: Filename path of the SSH key file to use.
        :type key_file: str
        :param allow_agent: Enable use of SSH key-agent.
        :type allow_agent: bool
        :param ssh_strict: Automatically reject unknown SSH host keys (default: False, which
                means unknown SSH host keys will be accepted).
        :type ssh_strict: bool
        :param system_host_keys: Load host keys from the user's 'known_hosts' file.
        :type system_host_keys: bool
        :param alt_host_keys: If `True` host keys will be loaded from the file specified in
                'alt_key_file'.
        :type alt_host_keys: bool
        :param alt_key_file: SSH host key file to use (if alt_host_keys=True).
        :type alt_key_file: str
        :param ssh_config_file: File name of OpenSSH configuration file.
        :type ssh_config_file: str
        :param timeout: Connection timeout.
        :type timeout: float
        :param session_timeout: Set a timeout for parallel requests.
        :type session_timeout: float
        :param keepalive: Send SSH keepalive packets at a specific interval, in seconds.
                Currently defaults to 0, for backwards compatibility (it will not attempt
                to keep the connection alive).
        :type keepalive: int
        """
        self.remote_conn = None
        self.RETURN = '\n'
        self.TELNET_RETURN = '\r\n'
        # Line Separator in response lines
        self.RESPONSE_RETURN = '\n'
        if ip:
            self.host = ip
            self.ip = ip
        elif host:
            self.host = host
        if not ip and not host:
            raise ValueError("Either ip or host must be set")
        if port is None:
            if 'telnet' in device_type:
                self.port = 23
            else:
                self.port = 22
        else:
            self.port = int(port)
        self.username = username
        self.password = password
        self.secret = secret
        self.device_type = device_type
        self.ansi_escape_codes = False
        self.verbose = verbose
        self.timeout = timeout
        self.session_timeout = session_timeout
        self.keepalive = keepalive

        # Use the greater of global_delay_factor or delay_factor local to method
        self.global_delay_factor = global_delay_factor

        # set in set_base_prompt method
        self.base_prompt = ''

        self._session_locker = Lock()
        # determine if telnet or SSH
        if '_telnet' in device_type:
            self.protocol = 'telnet'
            self._modify_connection_params()
            self.establish_connection()
            self.session_preparation()
        else:
            self.protocol = 'ssh'

            if not ssh_strict:
                self.key_policy = paramiko.AutoAddPolicy()
            else:
                self.key_policy = paramiko.RejectPolicy()

            # Options for SSH host_keys
            self.use_keys = use_keys
            self.key_file = key_file
            self.allow_agent = allow_agent
            self.system_host_keys = system_host_keys
            self.alt_host_keys = alt_host_keys
            self.alt_key_file = alt_key_file

            # For SSH proxy support
            self.ssh_config_file = ssh_config_file

            self._modify_connection_params()
            self.establish_connection()
            self.session_preparation()

    def __enter__(self):
        """Establish a session using a Context Manager."""
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """Gracefully close connection on Context Manager exit."""
        self.disconnect()
        if exc_type is not None:
            raise exc_type(exc_value)

    def _modify_connection_params(self):
        """Modify connection parameters prior to SSH connection."""
        pass

    def _timeout_exceeded(self, start, msg='Timeout exceeded!'):
        """
        Raise NetMikoTimeoutException if waiting too much in the
        serving queue.
        """
        if not start:
            # Must provide a comparison time
            return False
        if time.time() - start > self.session_timeout:
            # session_timeout exceeded
            raise NetMikoTimeoutException(msg)
        return False

    def _lock_netmiko_session(self, start=None):
        """
        Try to acquire the Netmiko session lock. If not available, wait in the queue until
        the channel is available again.
        """
        if not start:
            start = time.time()
        # Wait here until the SSH channel lock is acquired or until session_timeout exceeded
        while (not self._session_locker.acquire(False) and
               not self._timeout_exceeded(start, 'The netmiko channel is not available!')):
                time.sleep(.1)
        return True

    def _unlock_netmiko_session(self):
        """
        Release the channel at the end of the task.
        """
        if self._session_locker.locked():
            self._session_locker.release()

    def _write_channel(self, out_data):
        """Generic handler that will write to both SSH and telnet channel."""
        if self.protocol == 'ssh':
            self.remote_conn.sendall(write_bytes(out_data))
        elif self.protocol == 'telnet':
            self.remote_conn.write(write_bytes(out_data))
        else:
            raise ValueError("Invalid protocol specified")
        try:
            log.debug("write_channel: {}".format(write_bytes(out_data)))
        except UnicodeDecodeError:
            # Don't log non-ASCII characters; this is null characters and telnet IAC (PY2)
            pass

    def write_channel(self, out_data):
        """Generic handler that will write to both SSH and telnet channel."""
        self._lock_netmiko_session()
        try:
            self._write_channel(out_data)
        finally:
            # Always unlock the SSH channel, even on exception.
            self._unlock_netmiko_session()

    def is_alive(self):
        """Returns a boolean flag with the state of the connection."""
        null = chr(0)
        if self.remote_conn is None:
            log.error("Connection is not initialised, is_alive returns False")
            return False
        if self.protocol == 'telnet':
            try:
                # Try sending IAC + NOP (IAC is telnet way of sending command
                # IAC = Interpret as Command (it comes before the NOP)
                log.debug("Sending IAC + NOP")
                self.device.write_channel(telnetlib.IAC + telnetlib.NOP)
                return True
            except AttributeError:
                return False
        else:
            # SSH
            try:
                # Try sending ASCII null byte to maintain the connection alive
                log.debug("Sending the NULL byte")
                self.write_channel(null)
                return self.remote_conn.transport.is_active()
            except (socket.error, EOFError):
                log.error("Unable to send", exc_info=True)
                # If unable to send, we can tell for sure that the connection is unusable
                return False
        return False

    def _read_channel(self):
        """Generic handler that will read all the data from an SSH or telnet channel."""
        if self.protocol == 'ssh':
            output = ""
            while True:
                if self.remote_conn.recv_ready():
                    outbuf = self.remote_conn.recv(MAX_BUFFER)
                    if len(outbuf) == 0:
                        raise EOFError("Channel stream closed by remote device.")
                    output += outbuf.decode('utf-8', 'ignore')
                else:
                    break
        elif self.protocol == 'telnet':
            output = self.remote_conn.read_very_eager().decode('utf-8', 'ignore')
        log.debug("read_channel: {}".format(output))
        return output

    def read_channel(self):
        """Generic handler that will read all the data from an SSH or telnet channel."""
        output = ""
        self._lock_netmiko_session()
        try:
            output = self._read_channel()
        finally:
            # Always unlock the SSH channel, even on exception.
            self._unlock_netmiko_session()
        return output

    def _read_channel_expect(self, pattern='', re_flags=0, max_loops=None):
        """
        Function that reads channel until pattern is detected.

        pattern takes a regular expression.

        By default pattern will be self.base_prompt

        Note: this currently reads beyond pattern. In the case of SSH it reads MAX_BUFFER.
        In the case of telnet it reads all non-blocking data.

        There are dependencies here like determining whether in config_mode that are actually
        depending on reading beyond pattern.
        """
        output = ''
        if not pattern:
            pattern = re.escape(self.base_prompt)
        log.debug("Pattern is: {0}".format(pattern))

        # Will loop for self.timeout time (override with max_loops argument)
        i = 1
        loop_delay = .1
        if not max_loops:
            max_loops = self.timeout / loop_delay
        while i < max_loops:
            if self.protocol == 'ssh':
                try:
                    # If no data available will wait timeout seconds trying to read
                    self._lock_netmiko_session()
                    new_data = self.remote_conn.recv(MAX_BUFFER)
                    if len(new_data) == 0:
                        raise EOFError("Channel stream closed by remote device.")
                    new_data = new_data.decode('utf-8', 'ignore')
                    log.debug("_read_channel_expect read_data: {0}".format(new_data))
                    output += new_data
                except socket.timeout:
                    raise NetMikoTimeoutException("Timed-out reading channel, data not available.")
                finally:
                    self._unlock_netmiko_session()
            elif self.protocol == 'telnet':
                output += self.read_channel()
            if re.search(pattern, output, flags=re_flags):
                log.debug("Pattern found: {0} {1}".format(pattern, output))
                return output
            time.sleep(loop_delay * self.global_delay_factor)
            i += 1
        raise NetMikoTimeoutException("Timed-out reading channel, pattern not found in output: {}"
                                      .format(pattern))

    def _read_channel_timing(self, delay_factor=1, max_loops=150):
        """
        Read data on the channel based on timing delays.

        Attempt to read channel max_loops number of times. If no data this will cause a 15 second
        delay.

        Once data is encountered read channel for another two seconds (2 * delay_factor) to make
        sure reading of channel is complete.
        """
        delay_factor = self.select_delay_factor(delay_factor)
        channel_data = ""
        i = 0
        while i <= max_loops:
            time.sleep(.1 * delay_factor)
            new_data = self.read_channel()
            if new_data:
                channel_data += new_data
            else:
                # Safeguard to make sure really done
                time.sleep(2 * delay_factor)
                new_data = self.read_channel()
                if not new_data:
                    break
                else:
                    channel_data += new_data
            i += 1
        return channel_data

    def read_until_prompt(self, *args, **kwargs):
        """Read channel until self.base_prompt detected. Return ALL data available."""
        return self._read_channel_expect(*args, **kwargs)

    def read_until_pattern(self, *args, **kwargs):
        """Read channel until pattern detected. Return ALL data available."""
        return self._read_channel_expect(*args, **kwargs)

    def read_until_prompt_or_pattern(self, pattern='', re_flags=0):
        """Read until either self.base_prompt or pattern is detected. Return ALL data available."""
        combined_pattern = re.escape(self.base_prompt)
        if pattern:
            combined_pattern = r"({}|{})".format(combined_pattern, pattern)
        return self._read_channel_expect(combined_pattern, re_flags=re_flags)

    def telnet_login(self, pri_prompt_terminator='#', alt_prompt_terminator='>',
                     username_pattern=r"ser:|sername|ogin", pwd_pattern=r"assword",
                     delay_factor=1, max_loops=60):
        """Telnet login. Can be username/password or just password."""
        delay_factor = self.select_delay_factor(delay_factor)
        time.sleep(1 * delay_factor)

        output = ''
        return_msg = ''
        i = 1
        while i <= max_loops:
            try:
                output = self.read_channel()
                return_msg += output

                # Search for username pattern / send username
                if re.search(username_pattern, output):
                    self.write_channel(self.username + self.TELNET_RETURN)
                    time.sleep(1 * delay_factor)
                    output = self.read_channel()
                    return_msg += output

                # Search for password pattern / send password
                if re.search(pwd_pattern, output):
                    self.write_channel(self.password + self.TELNET_RETURN)
                    time.sleep(.5 * delay_factor)
                    output = self.read_channel()
                    return_msg += output
                    if pri_prompt_terminator in output or alt_prompt_terminator in output:
                        return return_msg

                # Check if proper data received
                if pri_prompt_terminator in output or alt_prompt_terminator in output:
                    return return_msg

                self.write_channel(self.TELNET_RETURN)
                time.sleep(.5 * delay_factor)
                i += 1
            except EOFError:
                msg = "Telnet login failed: {0}".format(self.host)
                raise NetMikoAuthenticationException(msg)

        # Last try to see if we already logged in
        self.write_channel(self.TELNET_RETURN)
        time.sleep(.5 * delay_factor)
        output = self.read_channel()
        return_msg += output
        if pri_prompt_terminator in output or alt_prompt_terminator in output:
            return return_msg

        msg = "Telnet login failed: {0}".format(self.host)
        raise NetMikoAuthenticationException(msg)

    def session_preparation(self):
        """
        Prepare the session after the connection has been established

        This method handles some differences that occur between various devices
        early on in the session.

        In general, it should include:
        self._test_channel_read()
        self.set_base_prompt()
        self.disable_paging()
        self.set_terminal_width()
        self.clear_buffer()
        """
        self._test_channel_read()
        self.set_base_prompt()
        self.disable_paging()
        self.set_terminal_width()

        # Clear the read buffer
        time.sleep(.3 * self.global_delay_factor)
        self.clear_buffer()

    def _use_ssh_config(self, dict_arg):
        """Update SSH connection parameters based on contents of SSH 'config' file."""

        connect_dict = dict_arg.copy()

        # Use SSHConfig to generate source content.
        full_path = path.abspath(path.expanduser(self.ssh_config_file))
        if path.exists(full_path):
            ssh_config_instance = paramiko.SSHConfig()
            with io.open(full_path, "rt", encoding='utf-8') as f:
                ssh_config_instance.parse(f)
                source = ssh_config_instance.lookup(self.host)
        else:
            source = {}

        if source.get('proxycommand'):
            proxy = paramiko.ProxyCommand(source['proxycommand'])
        elif source.get('ProxyCommand'):
            proxy = paramiko.ProxyCommand(source['proxycommand'])
        else:
            proxy = None

        # Only update 'hostname', 'sock', 'port', and 'username'
        # For 'port' and 'username' only update if using object defaults
        if connect_dict['port'] == 22:
            connect_dict['port'] = int(source.get('port', self.port))
        if connect_dict['username'] == '':
            connect_dict['username'] = source.get('username', self.username)
        if proxy:
            connect_dict['sock'] = proxy
        connect_dict['hostname'] = source.get('hostname', self.host)

        return connect_dict

    def _connect_params_dict(self):
        """Generate dictionary of Paramiko connection parameters."""
        conn_dict = {
            'hostname': self.host,
            'port': self.port,
            'username': self.username,
            'password': self.password,
            'look_for_keys': self.use_keys,
            'allow_agent': self.allow_agent,
            'key_filename': self.key_file,
            'timeout': self.timeout,
        }

        # Check if using SSH 'config' file mainly for SSH proxy support
        if self.ssh_config_file:
            conn_dict = self._use_ssh_config(conn_dict)
        return conn_dict

    def _sanitize_output(self, output, strip_command=False, command_string=None,
                         strip_prompt=False):
        """Sanitize the output."""
        if self.ansi_escape_codes:
            output = self.strip_ansi_escape_codes(output)
        output = self.normalize_linefeeds(output)
        if strip_command and command_string:
            output = self.strip_command(command_string, output)
        if strip_prompt:
            output = self.strip_prompt(output)
        return output

    def establish_connection(self, width=None, height=None):
        """
        Establish SSH connection to the network device

        Timeout will generate a NetMikoTimeoutException
        Authentication failure will generate a NetMikoAuthenticationException

        width and height are needed for Fortinet paging setting.
        """
        if self.protocol == 'telnet':
            self.remote_conn = telnetlib.Telnet(self.host, port=self.port, timeout=self.timeout)
            self.telnet_login()
        elif self.protocol == 'ssh':
            ssh_connect_params = self._connect_params_dict()
            self.remote_conn_pre = self._build_ssh_client()

            # initiate SSH connection
            try:
                self.remote_conn_pre.connect(**ssh_connect_params)
            except socket.error:
                msg = "Connection to device timed-out: {device_type} {ip}:{port}".format(
                    device_type=self.device_type, ip=self.host, port=self.port)
                raise NetMikoTimeoutException(msg)
            except paramiko.ssh_exception.AuthenticationException as auth_err:
                msg = "Authentication failure: unable to connect {device_type} {ip}:{port}".format(
                    device_type=self.device_type, ip=self.host, port=self.port)
                msg += self.RETURN + str(auth_err)
                raise NetMikoAuthenticationException(msg)

            if self.verbose:
                print("SSH connection established to {0}:{1}".format(self.host, self.port))

            # Use invoke_shell to establish an 'interactive session'
            if width and height:
                self.remote_conn = self.remote_conn_pre.invoke_shell(term='vt100', width=width,
                                                                     height=height)
            else:
                self.remote_conn = self.remote_conn_pre.invoke_shell()

            self.remote_conn.settimeout(self.timeout)
            if self.keepalive:
                self.remote_conn.transport.set_keepalive(self.keepalive)
            self.special_login_handler()
            if self.verbose:
                print("Interactive SSH session established")
        return ""

    def _test_channel_read(self, count=40, pattern=""):
        """Try to read the channel (generally post login) verify you receive data back."""
        def _increment_delay(main_delay, increment=1.1, maximum=8):
            """Increment sleep time to a maximum value."""
            main_delay = main_delay * increment
            if main_delay >= maximum:
                main_delay = maximum
            return main_delay

        i = 0
        delay_factor = self.select_delay_factor(delay_factor=0)
        main_delay = delay_factor * .1
        time.sleep(main_delay * 10)
        new_data = ""
        while i <= count:
            new_data += self._read_channel_timing()
            if new_data and pattern:
                if re.search(pattern, new_data):
                    break
            elif new_data:
                break
            else:
                self.write_channel(self.RETURN)
            main_delay = _increment_delay(main_delay)
            time.sleep(main_delay)
            i += 1

        # check if data was ever present
        if new_data:
            return ""
        else:
            raise NetMikoTimeoutException("Timed out waiting for data")

    def _build_ssh_client(self):
        """Prepare for Paramiko SSH connection."""
        # Create instance of SSHClient object
        remote_conn_pre = paramiko.SSHClient()

        # Load host_keys for better SSH security
        if self.system_host_keys:
            remote_conn_pre.load_system_host_keys()
        if self.alt_host_keys and path.isfile(self.alt_key_file):
            remote_conn_pre.load_host_keys(self.alt_key_file)

        # Default is to automatically add untrusted hosts (make sure appropriate for your env)
        remote_conn_pre.set_missing_host_key_policy(self.key_policy)
        return remote_conn_pre

    def select_delay_factor(self, delay_factor):
        """Choose the greater of delay_factor or self.global_delay_factor."""
        if delay_factor >= self.global_delay_factor:
            return delay_factor
        else:
            return self.global_delay_factor

    def special_login_handler(self, delay_factor=1):
        """Handler for devices like WLC, Avaya ERS that throw up characters prior to login."""
        pass

    def disable_paging(self, command="terminal length 0", delay_factor=1):
        """Disable paging default to a Cisco CLI method."""
        delay_factor = self.select_delay_factor(delay_factor)
        time.sleep(delay_factor * .1)
        self.clear_buffer()
        command = self.normalize_cmd(command)
        log.debug("In disable_paging")
        log.debug("Command: {0}".format(command))
        self.write_channel(command)
        output = self.read_until_prompt()
        if self.ansi_escape_codes:
            output = self.strip_ansi_escape_codes(output)
        log.debug("{0}".format(output))
        log.debug("Exiting disable_paging")
        return output

    def set_terminal_width(self, command="", delay_factor=1):
        """
        CLI terminals try to automatically adjust the line based on the width of the terminal.
        This causes the output to get distorted when accessed programmatically.

        Set terminal width to 511 which works on a broad set of devices.
        """
        if not command:
            return ""
        delay_factor = self.select_delay_factor(delay_factor)
        command = self.normalize_cmd(command)
        self.write_channel(command)
        output = self.read_until_prompt()
        if self.ansi_escape_codes:
            output = self.strip_ansi_escape_codes(output)
        return output

    def set_base_prompt(self, pri_prompt_terminator='#',
                        alt_prompt_terminator='>', delay_factor=1):
        """
        Sets self.base_prompt

        Used as delimiter for stripping of trailing prompt in output.

        Should be set to something that is general and applies in multiple contexts. For Cisco
        devices this will be set to router hostname (i.e. prompt without '>' or '#').

        This will be set on entering user exec or privileged exec on Cisco, but not when
        entering/exiting config mode.
        """
        prompt = self.find_prompt(delay_factor=delay_factor)
        if not prompt[-1] in (pri_prompt_terminator, alt_prompt_terminator):
            raise ValueError("Router prompt not found: {0}".format(repr(prompt)))
        # Strip off trailing terminator
        self.base_prompt = prompt[:-1]
        return self.base_prompt

    def find_prompt(self, delay_factor=1):
        """Finds the current network device prompt, last line only."""
        delay_factor = self.select_delay_factor(delay_factor)
        self.clear_buffer()
        self.write_channel(self.RETURN)
        time.sleep(delay_factor * .1)

        # Initial attempt to get prompt
        prompt = self.read_channel()
        if self.ansi_escape_codes:
            prompt = self.strip_ansi_escape_codes(prompt)

        # Check if the only thing you received was a newline
        count = 0
        prompt = prompt.strip()
        while count <= 10 and not prompt:
            prompt = self.read_channel().strip()
            if prompt:
                if self.ansi_escape_codes:
                    prompt = self.strip_ansi_escape_codes(prompt).strip()
            else:
                self.write_channel(self.RETURN)
                time.sleep(delay_factor * .1)
            count += 1

        # If multiple lines in the output take the last line
        prompt = self.normalize_linefeeds(prompt)
        prompt = prompt.split(self.RESPONSE_RETURN)[-1]
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("Unable to find prompt: {}".format(prompt))
        time.sleep(delay_factor * .1)
        self.clear_buffer()
        return prompt

    def clear_buffer(self):
        """Read any data available in the channel."""
        self.read_channel()

    def send_command_timing(self, command_string, delay_factor=1, max_loops=150,
                            strip_prompt=True, strip_command=True, normalize=True):
        """Execute command_string on the SSH channel using a delay-based mechanism. Generally
        used for show commands.

        :param command_string: The command to be executed on the remote device.
        :type command_string: str
        :param delay_factor: Multiplying factor used to adjust delays (default: 1).
        :type delay_factor: int
        :param max_loops: Controls wait time in conjunction with delay_factor (default: 150).
        :type max_loops: int
        :param strip_prompt: Remove the trailing router prompt from the output (default: True).
        :type strip_prompt: bool
        :param strip_command: Remove the echo of the command from the output (default: True).
        :type strip_command: bool
        :param normalize: Ensure the proper enter is sent at end of command (default: True).
        :type normalize: bool
        """
        output = ''
        delay_factor = self.select_delay_factor(delay_factor)
        self.clear_buffer()
        if normalize:
            command_string = self.normalize_cmd(command_string)

        self.write_channel(command_string)
        output = self._read_channel_timing(delay_factor=delay_factor, max_loops=max_loops)
        output = self._sanitize_output(output, strip_command=strip_command,
                                       command_string=command_string, strip_prompt=strip_prompt)
        return output

    def strip_prompt(self, a_string):
        """Strip the trailing router prompt from the output."""
        response_list = a_string.split(self.RESPONSE_RETURN)
        last_line = response_list[-1]
        if self.base_prompt in last_line:
            return self.RESPONSE_RETURN.join(response_list[:-1])
        else:
            return a_string

    def send_command(self, command_string, expect_string=None,
                     delay_factor=1, max_loops=500, auto_find_prompt=True,
                     strip_prompt=True, strip_command=True, normalize=True):
        """Execute command_string on the SSH channel using a pattern-based mechanism. Generally
        used for show commands. By default this method will keep waiting to receive data until the
        network device prompt is detected. The current network device prompt will be determined
        automatically.

        :param command_string: The command to be executed on the remote device.
        :type command_string: str
        :param expect_string: Regular expression pattern to use for determining end of output.
            If left blank will default to being based on router prompt.
        :type expect_str: str
        :param delay_factor: Multiplying factor used to adjust delays (default: 1).
        :type delay_factor: int
        :param max_loops: Controls wait time in conjunction with delay_factor (default: 150).
        :type max_loops: int
        :param strip_prompt: Remove the trailing router prompt from the output (default: True).
        :type strip_prompt: bool
        :param strip_command: Remove the echo of the command from the output (default: True).
        :type strip_command: bool
        :param normalize: Ensure the proper enter is sent at end of command (default: True).
        :type normalize: bool
        """
        delay_factor = self.select_delay_factor(delay_factor)

        # Find the current router prompt
        if expect_string is None:
            if auto_find_prompt:
                try:
                    prompt = self.find_prompt(delay_factor=delay_factor)
                except ValueError:
                    prompt = self.base_prompt
            else:
                prompt = self.base_prompt
            search_pattern = re.escape(prompt.strip())
        else:
            search_pattern = expect_string

        if normalize:
            command_string = self.normalize_cmd(command_string)

        time.sleep(delay_factor * .2)
        self.clear_buffer()
        self.write_channel(command_string)

        # Keep reading data until search_pattern is found (or max_loops)
        i = 1
        output = ''
        while i <= max_loops:
            new_data = self.read_channel()
            if new_data:
                output += new_data
                try:
                    lines = output.split(self.RETURN)
                    first_line = lines[0]
                    # First line is the echo line containing the command. In certain situations
                    # it gets repainted and needs filtered
                    if BACKSPACE_CHAR in first_line:
                        pattern = search_pattern + r'.*$'
                        first_line = re.sub(pattern, repl='', string=first_line)
                        lines[0] = first_line
                        output = self.RETURN.join(lines)
                except IndexError:
                    pass
                if re.search(search_pattern, output):
                    break
            else:
                time.sleep(delay_factor * .2)
            i += 1
        else:   # nobreak
            raise IOError("Search pattern never detected in send_command_expect: {0}".format(
                search_pattern))

        output = self._sanitize_output(output, strip_command=strip_command,
                                       command_string=command_string, strip_prompt=strip_prompt)
        return output

    def send_command_expect(self, *args, **kwargs):
        """Support previous name of send_command method."""
        return self.send_command(*args, **kwargs)

    @staticmethod
    def strip_backspaces(output):
        """Strip any backspace characters out of the output."""
        backspace_char = '\x08'
        return output.replace(backspace_char, '')

    def strip_command(self, command_string, output):
        """
        Strip command_string from output string

        Cisco IOS adds backspaces into output for long commands (i.e. for commands that line wrap)
        """
        backspace_char = '\x08'

        # Check for line wrap (remove backspaces)
        if backspace_char in output:
            output = output.replace(backspace_char, '')
            output_lines = output.split(self.RESPONSE_RETURN)
            new_output = output_lines[1:]
            return self.RESPONSE_RETURN.join(new_output)
        else:
            command_length = len(command_string)
            return output[command_length:]

    def normalize_linefeeds(self, a_string):
        """Convert `\r\r\n`,`\r\n`, `\n\r` to `\n.`"""
        newline = re.compile('(\r\r\r\n|\r\r\n|\r\n|\n\r)')
        a_string = newline.sub(self.RESPONSE_RETURN, a_string)
        if self.RESPONSE_RETURN == '\n':
            # Convert any remaining \r to \n
            return re.sub('\r', self.RESPONSE_RETURN, a_string)

    def normalize_cmd(self, command):
        """Normalize CLI commands to have a single trailing newline."""
        command = command.rstrip()
        command += self.RETURN
        return command

    def check_enable_mode(self, check_string=''):
        """Check if in enable mode. Return boolean."""
        self.write_channel(self.RETURN)
        output = self.read_until_prompt()
        return check_string in output

    def enable(self, cmd='', pattern='ssword', re_flags=re.IGNORECASE):
        """Enter enable mode."""
        output = ""
        if not self.check_enable_mode():
            self.write_channel(self.normalize_cmd(cmd))
            output += self.read_until_prompt_or_pattern(pattern=pattern, re_flags=re_flags)
            self.write_channel(self.normalize_cmd(self.secret))
            output += self.read_until_prompt()
            if not self.check_enable_mode():
                msg = "Failed to enter enable mode. Please ensure you pass " \
                      "the 'secret' argument to ConnectHandler."
                raise ValueError(msg)
        return output

    def exit_enable_mode(self, exit_command=''):
        """Exit enable mode."""
        output = ""
        if self.check_enable_mode():
            self.write_channel(self.normalize_cmd(exit_command))
            output += self.read_until_prompt()
            if self.check_enable_mode():
                raise ValueError("Failed to exit enable mode.")
        return output

    def check_config_mode(self, check_string='', pattern=''):
        """Checks if the device is in configuration mode or not."""
        self.write_channel(self.RETURN)
        output = self.read_until_pattern(pattern=pattern)
        return check_string in output

    def config_mode(self, config_command='', pattern=''):
        """Enter into config_mode."""
        output = ''
        if not self.check_config_mode():
            self.write_channel(self.normalize_cmd(config_command))
            output = self.read_until_pattern(pattern=pattern)
            if not self.check_config_mode():
                raise ValueError("Failed to enter configuration mode.")
        return output

    def exit_config_mode(self, exit_config='', pattern=''):
        """Exit from configuration mode."""
        output = ''
        if self.check_config_mode():
            self.write_channel(self.normalize_cmd(exit_config))
            output = self.read_until_pattern(pattern=pattern)
            if self.check_config_mode():
                raise ValueError("Failed to exit configuration mode")
        log.debug("exit_config_mode: {0}".format(output))
        return output

    def send_config_from_file(self, config_file=None, **kwargs):
        """
        Send configuration commands down the SSH channel from a file.

        The file is processed line-by-line and each command is sent down the
        SSH channel.

        **kwargs are passed to send_config_set method.
        """
        with io.open(config_file, "rt", encoding='utf-8') as cfg_file:
            return self.send_config_set(cfg_file, **kwargs)

    def send_config_set(self, config_commands=None, exit_config_mode=True, delay_factor=1,
                        max_loops=150, strip_prompt=False, strip_command=False):
        """
        Send configuration commands down the SSH channel.

        config_commands is an iterable containing all of the configuration commands.
        The commands will be executed one after the other.

        Automatically exits/enters configuration mode.
        """
        delay_factor = self.select_delay_factor(delay_factor)
        if config_commands is None:
            return ''
        elif isinstance(config_commands, string_types):
            config_commands = (config_commands,)

        if not hasattr(config_commands, '__iter__'):
            raise ValueError("Invalid argument passed into send_config_set")

        # Send config commands
        output = self.config_mode()
        for cmd in config_commands:
            self.write_channel(self.normalize_cmd(cmd))
            time.sleep(delay_factor * .5)

        # Gather output
        output += self._read_channel_timing(delay_factor=delay_factor, max_loops=max_loops)
        if exit_config_mode:
            output += self.exit_config_mode()
        output = self._sanitize_output(output)
        log.debug("{0}".format(output))
        return output

    def strip_ansi_escape_codes(self, string_buffer):
        """
        Remove any ANSI (VT100) ESC codes from the output

        http://en.wikipedia.org/wiki/ANSI_escape_code

        Note: this does not capture ALL possible ANSI Escape Codes only the ones
        I have encountered

        Current codes that are filtered:
        ESC = '\x1b' or chr(27)
        ESC = is the escape character [^ in hex ('\x1b')
        ESC[24;27H   Position cursor
        ESC[?25h     Show the cursor
        ESC[E        Next line (HP does ESC-E)
        ESC[K        Erase line from cursor to the end of line
        ESC[2K       Erase entire line
        ESC[1;24r    Enable scrolling from start to row end
        ESC[?6l      Reset mode screen with options 640 x 200 monochrome (graphics)
        ESC[?7l      Disable line wrapping
        ESC[2J       Code erase display
        ESC[00;32m   Color Green (30 to 37 are different colors) more general pattern is
                     ESC[\d\d;\d\dm and ESC[\d\d;\d\d;\d\dm

        HP ProCurve's, Cisco SG300, and F5 LTM's require this (possible others)
        """
        log.debug("In strip_ansi_escape_codes")
        log.debug("repr = {0}".format(repr(string_buffer)))

        code_position_cursor = chr(27) + r'\[\d+;\d+H'
        code_show_cursor = chr(27) + r'\[\?25h'
        code_next_line = chr(27) + r'E'
        code_erase_line_end = chr(27) + r'\[K'
        code_erase_line = chr(27) + r'\[2K'
        code_erase_start_line = chr(27) + r'\[K'
        code_enable_scroll = chr(27) + r'\[\d+;\d+r'
        code_form_feed = chr(27) + r'\[1L'
        code_carriage_return = chr(27) + r'\[1M'
        code_disable_line_wrapping = chr(27) + r'\[\?7l'
        code_reset_mode_screen_options = chr(27) + r'\[\?\d+l'
        code_erase_display = chr(27) + r'\[2J'
        code_graphics_mode = chr(27) + r'\[\d\d;\d\dm'
        code_graphics_mode2 = chr(27) + r'\[\d\d;\d\d;\d\dm'

        code_set = [code_position_cursor, code_show_cursor, code_erase_line, code_enable_scroll,
                    code_erase_start_line, code_form_feed, code_carriage_return,
                    code_disable_line_wrapping, code_erase_line_end,
                    code_reset_mode_screen_options, code_erase_display,
                    code_graphics_mode, code_graphics_mode2]

        output = string_buffer
        for ansi_esc_code in code_set:
            output = re.sub(ansi_esc_code, '', output)

        # CODE_NEXT_LINE must substitute with return
        output = re.sub(code_next_line, self.RETURN, output)

        log.debug("new_output = {0}".format(output))
        log.debug("repr = {0}".format(repr(output)))

        return output

    def cleanup(self):
        """Any needed cleanup before closing connection."""
        pass

    def disconnect(self):
        """Gracefully close the SSH connection."""
        self.cleanup()
        if self.protocol == 'ssh':
            self.remote_conn_pre.close()
        elif self.protocol == 'telnet':
            self.remote_conn.close()
        self.remote_conn = None

    def commit(self):
        """Commit method for platforms that support this."""
        raise AttributeError("Network device does not support 'commit()' method")


class TelnetConnection(BaseConnection):
    pass

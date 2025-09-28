from __future__ import annotations

import sys
import paramiko
import getpass
from typing import Optional
from pathlib import Path

from .utils import ExecResult, ToolError, info, warn, err, quote_for_shell

class SSHSession:
    """
    Simple wrapper around paramiko for executing commands and SFTP transfers.
    Prefers SSH keys (~/.ssh) and agent. Falls back to password when needed.
    """

    def __init__(self, hostname: str, username: str, port: int = 22, verbose: bool = False) -> None:
        self.hostname = hostname
        self.username = username
        self.port = port
        self.verbose = verbose
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.sftp: Optional[paramiko.SFTPClient] = None

    def connect(self) -> None:
        """
        Try connecting with look_for_keys first.
        If AuthenticationException occurs, prompt for password and retry.
        """
        try:
            if self.verbose:
                info("Attempting key-based auth (~/.ssh)...")
            # XXX: allow_agent=True will make look_for_keys=True failed
            self.client.connect(
                hostname=self.hostname,
                username=self.username,
                port=self.port,
                allow_agent=False,
                look_for_keys=True,
                timeout=20,
            )
        except paramiko.AuthenticationException:
            warn("Key-based auth failed. Falling back to password auth.")
            password = getpass.getpass(f"Password for {self.username}@{self.hostname}: ")
            try:
                self.client.connect(
                    hostname=self.hostname,
                    username=self.username,
                    port=self.port,
                    password=password,
                    allow_agent=False,
                    look_for_keys=False,
                    timeout=20,
                )
            except paramiko.AuthenticationException as e:
                raise ToolError("Authentication failed. Please verify credentials or keys.") from e
        except Exception as e:
            raise ToolError(f"SSH connection error: {e}") from e

        # Prepare SFTP
        try:
            self.sftp = self.client.open_sftp()
        except Exception as e:
            raise ToolError(f"Failed to open SFTP channel: {e}") from e

        if self.verbose:
            info("SSH connected and SFTP channel opened.")

    def close(self) -> None:
        try:
            if self.sftp:
                self.sftp.close()
        finally:
            self.client.close()

    def exec(self, command: str, timeout: int = 0) -> ExecResult:
        """
        Execute a remote command within bash -lc to normalize quoting.
        Returns ExecResult with stdout/stderr and exit code.
        """
        wrapped = f"bash -lc {quote_for_shell(command)}"
        if self.verbose:
            info(f"Executing on remote: {command}")
        try:
            stdin, stdout, stderr = self.client.exec_command(wrapped, timeout=timeout or None)
            out = stdout.read().decode("utf-8", errors="replace")
            err_ = stderr.read().decode("utf-8", errors="replace")
            exit_status = stdout.channel.recv_exit_status()
            if self.verbose:
                info(f"Exit: {exit_status}")
                if out.strip():
                    print(out)
                if err_.strip():
                    print(err_, file=sys.stderr)
            return ExecResult(exit_status=exit_status, stdout=out, stderr=err_)
        except Exception as e:
            raise ToolError(f"Remote command failed: {e}") from e

    def sftp_get(self, remote_path: str, local_path: Path) -> None:
        if not self.sftp:
            raise ToolError("SFTP channel is not available.")
        try:
            self.sftp.get(remote_path, str(local_path))
        except Exception as e:
            raise ToolError(f"SFTP get failed for {remote_path}: {e}") from e


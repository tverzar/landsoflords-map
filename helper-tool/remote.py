"""SSH-based control of a remote continent_scan_cli.py running in tmux.

Deliberately never handles the game password itself — the GUI collects it
from the user into its own masked field, and send_password() just forwards
whatever string it's given to the remote tmux pane, the same way a person
typing at an SSH terminal would. This module only ever sees an SSH key or
an SSH password the user supplies for *their own server*, same as any SSH
client.
"""
from pathlib import Path

import paramiko

SESSION = "lolscan"
REMOTE_DIR = "~/lol-scan"


class RemoteError(Exception):
    pass


class RemoteScanManager:
    def __init__(self, host, username="root", key_path=None, password=None, port=22):
        self.host = host
        self.username = username
        self.key_path = key_path
        self.password = password
        self.port = port
        self.client = None

    def connect(self, timeout=15):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self.client.connect(
                self.host, port=self.port, username=self.username,
                key_filename=self.key_path, password=self.password,
                timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
            )
        except Exception as e:
            self.client = None
            raise RemoteError(f"Не удалось подключиться: {e}") from e

    def close(self):
        if self.client:
            self.client.close()
            self.client = None

    @property
    def connected(self):
        return self.client is not None and self.client.get_transport() is not None and self.client.get_transport().is_active()

    def run(self, command, timeout=25):
        if not self.connected:
            raise RemoteError("Нет подключения")
        _stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        return code, out, err

    def _sftp_mkdir_p(self, sftp, path):
        try:
            sftp.stat(path)
        except FileNotFoundError:
            sftp.mkdir(path)

    def ensure_remote_setup(self):
        """tmux + python3 present, base directories exist."""
        code, out, _ = self.run("which tmux python3 && echo OK")
        if "OK" not in out:
            raise RemoteError("На сервере не найден tmux или python3 — установите вручную (apt install tmux python3).")
        self.run(f"mkdir -p {REMOTE_DIR}/profile_data {REMOTE_DIR}/www")

    def deploy_files(self, local_dir, filenames=("lol_api.py", "continent_scan_cli.py", "build_map_artifact.py", "styles.py")):
        if not self.connected:
            raise RemoteError("Нет подключения")
        sftp = self.client.open_sftp()
        try:
            for name in filenames:
                local_path = Path(local_dir) / name
                if local_path.exists():
                    sftp.put(str(local_path), f"{REMOTE_DIR}/{name}")
        finally:
            sftp.close()

    def find_remote_state_filename(self):
        """Which continent_*.json the remote scan is actually writing to —
        asking the server directly instead of guessing the domain
        client-side, which was wrong whenever X/Y wasn't typed in by hand."""
        code, out, _err = self.run(f"ls -t {REMOTE_DIR}/profile_data/continent_*.json 2>/dev/null | head -1")
        name = out.strip()
        return Path(name).name if name else None

    def pull_state_file(self, local_path, remote_filename):
        if not self.connected:
            raise RemoteError("Нет подключения")
        sftp = self.client.open_sftp()
        try:
            sftp.get(f"{REMOTE_DIR}/profile_data/{remote_filename}", str(local_path))
        finally:
            sftp.close()

    def tmux_session_exists(self):
        code, _out, _err = self.run(f"tmux has-session -t {SESSION} 2>/dev/null")
        return code == 0

    def ensure_tmux_session(self):
        if not self.tmux_session_exists():
            self.run(f"tmux new-session -d -s {SESSION} -c {REMOTE_DIR}")

    @staticmethod
    def _tmux_escape(s):
        return "'" + s.replace("'", "'\\''") + "'"

    def send_command(self, command):
        self.ensure_tmux_session()
        self.run(f"tmux send-keys -t {SESSION} {self._tmux_escape(command)} Enter")

    def send_password(self, password):
        """Forwards a password the *user* typed into the GUI to the remote
        tmux pane's waiting getpass prompt — same as them typing it over
        SSH themselves. This code never originates or stores the value."""
        self.run(f"tmux send-keys -t {SESSION} {self._tmux_escape(password)} Enter")

    def send_interrupt(self):
        self.run(f"tmux send-keys -t {SESSION} C-c")

    def is_busy(self):
        """True if the tmux pane looks mid-run (or waiting on a password
        prompt) rather than sitting at an idle shell — sending a new start
        command in that state would land as garbage keystrokes into
        whatever's already running instead of actually starting anything."""
        if not self.tmux_session_exists():
            return False
        log = self.capture_log(lines=3)
        lines = [l for l in log.splitlines() if l.strip()]
        if not lines:
            return False
        import re
        return not re.search(r"[#$]\s*$", lines[-1])

    def start_scan(self, username, extra_args=""):
        cmd = f'python3 continent_scan_cli.py --username "{username}"'
        if extra_args:
            cmd += f" {extra_args}"
        self.send_command(cmd)

    def capture_log(self, lines=30):
        if not self.tmux_session_exists():
            return ""
        code, out, _err = self.run(f"tmux capture-pane -t {SESSION} -p -S -{lines}")
        return out

    def setup_static_server(self):
        """Idempotent: local http.server on 127.0.0.1:8080 serving www/."""
        code, out, _err = self.run("pgrep -f 'http.server 8080' || true")
        if not out.strip():
            self.run(
                f"cd {REMOTE_DIR}/www && (nohup python3 -m http.server 8080 --bind 127.0.0.1 "
                f"> {REMOTE_DIR}/www/http.log 2>&1 &)"
            )

    def rebuild_map(self, friendly=""):
        cmd = f"cd {REMOTE_DIR} && python3 build_map_artifact.py -o www/index.html"
        if friendly:
            cmd += f" --friendly {self._tmux_escape(friendly)}"
        code, out, err = self.run(cmd, timeout=60)
        if code != 0:
            raise RemoteError(err or out or "build_map_artifact.py failed")
        return out

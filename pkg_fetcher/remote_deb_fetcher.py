from __future__ import annotations

import os
from typing import List
from .sshsession import SSHSession
from .utils import ToolError, info, warn, err, shlex_quote

class RemoteDebFetcher:
    """
    Encapsulates remote operations to compute dependency list and download .deb files
    into a remote temporary directory, then list those files for transfer.
    """

    def __init__(self, ssh: SSHSession, non_interactive_yes: bool = False, verbose: bool = False) -> None:
        self.ssh = ssh
        self.yes = non_interactive_yes
        self.verbose = verbose

    def assert_apt_host(self) -> None:
        """Ensure apt-get exists on remote."""
        res = self.ssh.exec("command -v apt-get >/dev/null 2>&1; echo $?")
        if res.stdout.strip() != "0":
            raise ToolError("Remote host does not have apt-get. This tool requires a Debian/Ubuntu-like system.")

    def mktemp_dir(self) -> str:
        """Create a unique remote temp directory and return its path."""
        res = self.ssh.exec("mktemp -d /tmp/debfetch-XXXXXXXX")
        if res.exit_status != 0:
            raise ToolError("Failed to create remote temp directory.")
        tmp = res.stdout.strip()
        if self.verbose:
            info(f"Remote temp dir: {tmp}")
        return tmp

    def cleanup_dir(self, path: str) -> None:
        """Remove remote directory recursively."""
        _ = self.ssh.exec(f"rm -rf {shlex_quote(path)}")

    def choose_downloader(self) -> str:
        """
        Prefer wget, fallback to curl. Return command template:
        - 'wget -q -P {dir} {url}'
        - 'curl -L --fail --silent --show-error -o {dir}/{filename} {url}'
        """
        if self.ssh.exec("command -v wget >/dev/null 2>&1").exit_status == 0:
            return "wget -q -P {dir} {url}"
        if self.ssh.exec("command -v curl >/dev/null 2>&1").exit_status == 0:
            return "curl -L --fail --silent --show-error -o {dir}/{fname} {url}"
        raise ToolError("Neither wget nor curl found on remote host.")

    # ---------- Strategy 1: apt-get --print-uris (preferred) ----------

    def compute_uris_via_apt_print(self, package: str) -> List[str]:
        """
        Use 'apt-get --print-uris --yes -o Debug::NoLocking=1 --no-install-recommends install <pkg>'
        to derive all .deb URLs *without installing*.
        This usually does not require root.
        """
        # Note: use \047 as single-quote in awk pattern
        cmd = (
            "set -o pipefail; "
            f"apt-get --print-uris --yes -o Debug::NoLocking=1 --no-install-recommends install {shlex_quote(package)} "
            r"2>/dev/null | awk -F\"'\" '/^'\''http/ {print $2}'"
        )
        res = self.ssh.exec(cmd)
        if res.exit_status != 0:
            raise ToolError("Failed to compute URIs via apt-get --print-uris.")
        uris = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        if self.verbose:
            info(f"URIs via print-uris: {len(uris)}")
        # Must include the package itself even if already installed
        if not uris and not self.yes:
            warn("No URIs returned by print-uris. The package name may be invalid or the apt lists are stale.")
        return uris

    # ---------- Strategy 2: apt-rdepends + apt-get download ----------

    def ensure_apt_rdepends(self) -> bool:
        """Check if apt-rdepends is available on remote."""
        return self.ssh.exec("command -v apt-rdepends >/dev/null 2>&1").exit_status == 0

    def compute_packages_via_rdepends(self, package: str) -> List[str]:
        """
        Use apt-rdepends to expand dependency set (required only), deduplicate, and return package names.
        """
        # Exclude optional relationships; keep hard Depends/PreDepends
        cmd = (
            "set -euo pipefail; "
            f"apt-rdepends -p {shlex_quote(package)} 2>/dev/null | grep -v '^ '"
        )
        res = self.ssh.exec(cmd)
        if res.exit_status != 0:
            raise ToolError("apt-rdepends failed to compute dependencies.")
        pkgs = [x.strip() for x in res.stdout.splitlines() if x.strip()]
        if self.verbose:
            info(f"Packages via apt-rdepends: {len(pkgs)}")
        return pkgs

    # ---------- Strategy 3: apt-cache depends (fallback) ----------

    def compute_packages_via_apt_cache(self, package: str) -> List[str]:
        """
        Use apt-cache depends --recurse to approximate the required dependencies set.
        """
        cmd = (
            "set -euo pipefail; "
            f"apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts "
            f"--no-breaks --no-replaces --no-enhances {shlex_quote(package)} | "
            r"grep -E '^\s*(Pre)?Depends:' | sed -E 's/.*Depends:\s+//' | sed -E 's/\s*\(.*\)//' | sed -E 's/\s*\|.*$//' | "
            r"cat <(echo " + shlex_quote(package) + r") - | awk 'NF' | sort -u"
        )
        res = self.ssh.exec(cmd)
        if res.exit_status != 0:
            raise ToolError("apt-cache depends failed to compute dependencies.")
        pkgs = [x.strip() for x in res.stdout.splitlines() if x.strip()]
        if self.verbose:
            info(f"Packages via apt-cache: {len(pkgs)}")
        return pkgs

    # ---------- Download helpers ----------

    def download_uris(self, uris: List[str], remote_dir: str) -> None:
        """Download each URI to remote_dir using wget or curl."""
        if not uris:
            raise ToolError("Empty URI list; nothing to download.")
        tmpl = self.choose_downloader()
        # Build a safe loop to download one-by-one to have clear error points
        # We also derive filename if using curl template.
        loop = [
            (
                tmpl.format(
                    dir=shlex_quote(remote_dir),
                    url=shlex_quote(u),
                    fname=shlex_quote(os.path.basename(u)),
                )
            )
            for u in uris
        ]
        cmd = "set -e; " + " ; ".join(loop)
        res = self.ssh.exec(cmd)
        if res.exit_status != 0:
            raise ToolError("Failed to download one or more .deb files via URIs.")

    def download_packages(self, packages: List[str], remote_dir: str) -> None:
        """Use apt-get download for each package into remote_dir (no root needed)."""
        if not packages:
            raise ToolError("Empty package list; nothing to download.")
        cmd = (
            f"set -e; cd {shlex_quote(remote_dir)}; "
            "for p in " + " ".join(map(shlex_quote, packages)) + "; do "
            "  apt-get download \"$p\"; "
            "done"
        )
        res = self.ssh.exec(cmd)
        if res.exit_status != 0:
            raise ToolError("apt-get download failed for one or more packages.")

    def list_debs(self, remote_dir: str) -> List[str]:
        """List all .deb files inside remote_dir and return absolute paths."""
        cmd = f"find {shlex_quote(remote_dir)} -maxdepth 1 -type f -name '*.deb' -print"
        res = self.ssh.exec(cmd)
        if res.exit_status != 0:
            raise ToolError("Failed to enumerate downloaded .deb files.")
        files = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        if self.verbose:
            info(f"Remote deb files: {len(files)}")
        return files
